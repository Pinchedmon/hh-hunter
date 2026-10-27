"""Всё общение с hh.ru — через настоящий Chrome под твоим логином.

Публичный api.hh.ru анонимам отдаёт 403, а curl ловит капчу, поэтому:
запускаем обычный Chrome с отдельным профилем и цепляемся к нему по CDP
(без флага webdriver — для сайта это просто ты в браузере).
Поиск и карточки вакансий читаем fetch'ем изнутри страницы hh,
отклик делаем как человек: форма → письмо → «Откликнуться».
"""
import asyncio
import json
import os
import random
import subprocess
import urllib.request
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Browser, Page

from .config import CDP_PORT, CHROME, CHROME_PROFILE, HH, SEARCH_MAX_PAGES
from . import db, guard

CAPTCHA_MARKERS = ("Пройдите капчу", "Подтвердите, что вы не робот", "captcha")


def _alarm(status: int, captcha: bool, where: str):
    """Любой сигнал недоверия от hh → общая пауза всех запросов."""
    if captcha or status in (403, 429):
        until = guard.trip(f"{'капча' if captcha else f'HTTP {status}'} на {where}")
        raise guard.Blocked("hh насторожился — пауза", until)

_pw = None
_browser: Browser | None = None
_page: Page | None = None
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost мимо прокси
lock = asyncio.Lock()  # одна вкладка — одно действие за раз


def _cdp_alive() -> bool:
    try:
        _local.open(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
        return True
    except Exception:
        return False


async def page() -> Page:
    global _pw, _browser, _page
    if _page and not _page.is_closed():
        return _page
    if not _cdp_alive():
        if not CHROME:
            raise RuntimeError("не нашёл Google Chrome — установи его и перезапусти программу")
        CHROME_PROFILE.mkdir(exist_ok=True)
        subprocess.Popen(
            [CHROME, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={CHROME_PROFILE}",
             "--no-first-run", "--no-default-browser-check", f"{HH}/applicant/resumes"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            await asyncio.sleep(0.25)
            if _cdp_alive():
                break
    _pw = _pw or await async_playwright().start()
    # ws-адрес берём сами: http-запрос драйвера Playwright уходит в системный прокси и ловит 503
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    ws = json.load(_local.open(f"http://127.0.0.1:{CDP_PORT}/json/version"))["webSocketDebuggerUrl"]
    _browser = await _pw.chromium.connect_over_cdp(ws)
    ctx = _browser.contexts[0]
    _page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    if not _page.url.startswith(HH):
        await _page.goto(f"{HH}/applicant/resumes", wait_until="domcontentloaded")
    return _page


async def _goto(p: Page, url: str, kind: str | None = "read"):
    """Навигация через предохранитель. hh иногда обрывает переход (редиректы/SPA) — пробуем ещё раз."""
    if kind:
        await guard.before(kind)
    for attempt in range(3):
        try:
            resp = await p.goto(url, wait_until="domcontentloaded")
            break
        except Exception:
            if attempt == 2:
                raise
            await p.wait_for_timeout(1500)
    await p.wait_for_timeout(600)
    await _accept_cookies(p)
    body = await p.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''")
    _alarm(resp.status if resp else 200, "captcha" in p.url or any(m in body for m in CAPTCHA_MARKERS[:2]), url.split("hh.ru")[-1][:40])
    return resp


async def _accept_cookies(p: Page):
    """Плашка про cookies перекрывает форму отклика — закрываем, как закрыл бы человек."""
    try:
        btn = p.locator('[data-qa="cookies-policy-informer-accept"]')
        if await btn.count() and await btn.first.is_visible():
            await btn.first.click()
            await p.wait_for_timeout(300)
    except Exception:
        pass


async def status() -> dict:
    """Залогинен ли ты и какие у тебя резюме."""
    async with lock:
        p = await page()
        await guard.before("read")
        info = await p.evaluate("""async () => {
            const h = await (await fetch('/applicant/resumes')).text();
            const d = new DOMParser().parseFromString(h, 'text/html');
            const logged = !/Войти|login/i.test(d.querySelector('[data-qa="login"]')?.textContent || '') && !h.includes('data-qa="login"');
            const titles = [...d.querySelectorAll('[data-qa="resume-title"], [data-qa="resume-title-link"]')].map(e => e.textContent.trim());
            return {logged, resumes: [...new Set(titles)]};
        }""")
    return info


async def open_login():
    """Показать окно Chrome со страницей входа hh — логинишься руками, мы пароль не видим."""
    async with lock:
        p = await page()
        await _goto(p, f"{HH}/account/login", kind=None)
        await p.bring_to_front()


async def suggest_areas(q: str) -> list[dict]:
    """Подсказка городов hh (тот же endpoint, что у поля «Город» на сайте)."""
    p = await page()
    return await p.evaluate("""async (q) => {
        const r = await fetch('/autosuggest/multiprefix/v2?d=areas_RU&q=' + encodeURIComponent(q));
        if (!r.ok) return [];
        const j = await r.json();
        return (j.items || []).slice(0, 8).map(i => ({id: String(i.id), name: i.text,
            region: i.parent ? i.parent.text : ''}));
    }""", q)


# ---------- поиск ----------

SERP_JS = """async (qs) => {
  const r = await fetch('/search/vacancy?' + qs);
  const h = await r.text();
  const d = new DOMParser().parseFromString(h, 'text/html');
  if (r.status !== 200) return {status: r.status, captcha: false, items: []};
  // капча = нет ни выдачи, ни плашки «ничего не найдено», зато есть форма капчи
  if (!d.querySelector('[data-qa="vacancy-serp__results"]') && d.querySelector('[data-qa*="captcha"], form[action*="captcha"], img[src*="captcha"]'))
    return {status: r.status, captcha: true, items: []};
  const items = [...d.querySelectorAll('[data-qa="vacancy-serp__vacancy"]')].map(c => {
    const a = c.querySelector('[data-qa="serp-item__title"]');
    const id = (a?.getAttribute('href') || '').match(/vacancy\\/(\\d+)/)?.[1];
    return {id, title: a?.textContent.trim(),
            employer: c.querySelector('[data-qa="vacancy-serp__vacancy-employer-text"]')?.textContent.trim() || ''};
  }).filter(x => x.id);
  return {status: r.status, captcha: false, items};
}"""


async def search(s: dict, max_pages=SEARCH_MAX_PAGES, period_days=30) -> list[dict]:
    params = {k: v for k, v in s.items() if k != "name" and v}
    params |= {"order_by": "publication_time", "search_period": period_days, "items_on_page": 50}
    out: dict[str, dict] = {}
    async with lock:
        p = await page()
        for pg in range(max_pages):
            await guard.before("read")
            r = await p.evaluate(SERP_JS, urlencode({**params, "page": pg}))
            _alarm(r["status"], r["captcha"], "поиске")
            if not r["items"]:
                break
            for it in r["items"]:
                out.setdefault(it["id"], it)
            if len(r["items"]) < 20:  # неполная страница — дальше пусто, лишний запрос не делаем
                break
    return list(out.values())


# ---------- карточка вакансии ----------

DETAIL_JS = """async (id) => {
  const r = await fetch('/vacancy/' + id);
  const h = await r.text();
  const d = new DOMParser().parseFromString(h, 'text/html');
  const captcha = !d.querySelector('[data-qa="vacancy-title"]') && !!d.querySelector('[data-qa*="captcha"], form[action*="captcha"], img[src*="captcha"]');
  const g = q => d.querySelector(`[data-qa="${q}"]`)?.textContent.replace(/\\s+/g, ' ').trim() || '';
  const n = h.replace(/&#34;|&quot;|\\\\"/g, '"');
  const desc = d.querySelector('[data-qa="vacancy-description"]');
  return {
    status: r.status, captcha,
    title: g('vacancy-title'), employer: g('vacancy-company-name'), salary: g('vacancy-salary'),
    experience: g('vacancy-experience'),
    work_format: (g('work-formats-text') || g('vacancy-view-employment-mode')).replace('Формат работы: ', ''),
    address: g('vacancy-view-raw-address') || g('vacancy-view-location'),
    description: desc ? desc.innerText || desc.textContent : '',
    skills: [...d.querySelectorAll('[data-qa="skills-element"]')].map(e => e.textContent.trim()).join(', '),
    letter_required: /@responseLetterRequired"\\s*:\\s*true/.test(n) ? 1 : 0,
    has_test: /hasTests"\\s*:\\s*true/.test(n) ? 1 : 0,
    archived: /"archived"\\s*:\\s*true/.test(n),
  };
}"""


async def vacancy(vid: str) -> dict:
    async with lock:
        p = await page()
        await guard.before("read")
        d = await p.evaluate(DETAIL_JS, vid)
        _alarm(d.pop("status"), d.pop("captcha"), f"вакансии {vid}")
    d["description"] = " ".join(d["description"].split())[:6000]
    return d


# ---------- отклик ----------

async def apply(vid: str, letter: str, resume: dict | None) -> tuple[str, str]:
    """resume = {hash, title}. Возвращает (status, note): sent | needs_me | error."""
    async with lock:
        p = await page()
        await _goto(p, f"{HH}/applicant/vacancy_response?vacancyId={vid}", kind=None)  # бюджет «send» списал sender
        try:
            await p.wait_for_selector("text=Резюме для отклика", timeout=15000)
        except Exception:
            body = await p.inner_text("body")
            if "уже откликались" in body or "Вы откликнулись" in body:
                return "sent", "уже был отклик"
            if "капч" in body.lower():
                guard.trip("капча на форме отклика")
                return "needs_me", "капча — пройди в окне Chrome и нажми «Отправить» ещё раз"
            return "error", "форма отклика не открылась: " + body[:150]
        await p.wait_for_timeout(1200)  # анкета работодателя дорисовывается позже

        # Анкета / тест / вопросы работодателя — это руками
        questions = await p.evaluate("""() => !!document.querySelector('[data-qa="employer-asking-for-test"], [data-qa="task-body"]')
            || document.querySelectorAll('textarea').length > 1
            || document.querySelectorAll('input[type=radio], input[type=checkbox]').length > 0""")
        if questions:
            return "needs_me", "у работодателя анкета/тест — ответь сам (письмо скопируй из карточки)"

        # Выбираем нужное резюме в выпадашке формы
        if resume:
            current = (await p.inner_text('[data-qa="resume-title"]')).strip()
            if current != resume["title"]:
                await p.locator('[data-qa="resume-title"]').first.click()
                opt = p.locator(f'[data-qa="magritte-select-option-{resume["hash"]}"]')
                await opt.wait_for(timeout=5000)
                await opt.click()
                await p.wait_for_timeout(700)
                current = (await p.inner_text('[data-qa="resume-title"]')).strip()
                if current != resume["title"]:
                    return "needs_me", f"не смог выбрать резюме «{resume['title']}» (в форме «{current}»)"
            if await p.query_selector('[data-qa="hidden-resume-warning"]:visible'):
                return "needs_me", f"резюме «{resume['title']}» скрыто — сделай его видимым на hh"

        if not await p.query_selector("textarea"):
            await p.get_by_text("Добавить", exact=True).first.click()
            await p.wait_for_selector("textarea", timeout=5000)
        await p.fill("textarea", letter)
        await p.wait_for_timeout(random.randint(600, 1400))
        await p.get_by_role("button", name="Откликнуться", exact=True).click()

        for _ in range(20):
            await p.wait_for_timeout(500)
            body = await p.inner_text("body")
            if any(s in body for s in ("Отклик отправлен", "Вы откликнулись", "Резюме доставлено")):
                await p.keyboard.press("Escape")  # hh после отклика лезет с опросником «формат/зарплата»
                return "sent", ""
            if "Пройдите капчу" in body:
                guard.trip("капча при отклике")
                return "needs_me", "капча после нажатия — введи её в окне Chrome, письмо уже вставлено"
        return "error", "не дождался подтверждения отклика"


# ---------- ответы работодателей ----------

NEGOTIATIONS_JS = """async (pg) => {
  const r = await fetch('/applicant/negotiations?filter=all&page=' + pg);
  const h = await r.text();
  const d = new DOMParser().parseFromString(h, 'text/html');
  const captcha = !d.querySelector('[data-qa="negotiations-list"], [data-qa="tab-list"]') && /captcha|не робот/i.test(h);
  const items = [...d.querySelectorAll('[data-qa="negotiations-item"]')].map(it => {
    const href = it.querySelector('a[href*="/vacancy/"]')?.getAttribute('href') || '';
    const tags = [...it.querySelectorAll('[data-qa^="negotiations-tag"]')]
      .map(t => t.getAttribute('data-qa').split(' ').pop().replace('negotiations-item-', ''));
    return {id: (href.match(/vacancy\\/(\\d+)/) || [])[1], state: tags[0] || ''};
  }).filter(x => x.id);
  return {status: r.status, captcha, items};
}"""


async def negotiations(max_pages=5) -> dict[str, str]:
    """Статусы откликов с hh: vacancy_id → not-viewed | viewed | discard | invitation | interview | hired."""
    out: dict[str, str] = {}
    async with lock:
        p = await page()
        for pg in range(max_pages):
            await guard.before("read")
            r = await p.evaluate(NEGOTIATIONS_JS, pg)
            _alarm(r["status"], r["captcha"], "откликах")
            for it in r["items"]:
                out.setdefault(it["id"], it["state"])
            if len(r["items"]) < 10:
                break
    return out


# ---------- твои резюме ----------

async def list_resumes() -> list[dict]:
    """Все резюме с hh: hash, название, видимость, полный текст."""
    async with lock:
        p = await page()
        await _goto(p, f"{HH}/applicant/resumes")
        await p.wait_for_selector('[data-qa^="resume-card-link-"]', timeout=20000)
        cards = await p.evaluate("""() => {
            const hidden = new Set([...document.querySelectorAll('a[href*="/visibility"]')]
                .map(a => a.getAttribute('href').split('/')[3]));
            return [...document.querySelectorAll('[data-qa^="resume-card-link-"]')].map(a => {
                const hash = a.getAttribute('data-qa').replace('resume-card-link-', '');
                return {hash, visible: !hidden.has(hash)};
            });
        }""")
        out = []
        for c in cards:
            await _goto(p, f"{HH}/resume/{c['hash']}")
            await p.wait_for_selector("main", timeout=20000)
            await p.wait_for_timeout(800)
            text = await p.inner_text("main")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            title = lines[lines.index("/") + 1] if "/" in lines[:3] else (await p.title()).strip()
            # контакты в LLM не отдаём — они ему не нужны
            if "Контакты" in text and "Опыт работы" in text:
                text = text[:text.index("Контакты")] + text[text.index("Опыт работы"):]
            # обрезаем хвост страницы (курсы, подборки hh) — нужен только сам текст резюме
            text = text.split("Курсы от проверенных школ")[0].split("Завершённость резюме")[0]
            out.append({**c, "title": title, "text": text.strip()[:8000]})
        await _goto(p, f"{HH}/applicant/resumes", kind=None)
    return out
