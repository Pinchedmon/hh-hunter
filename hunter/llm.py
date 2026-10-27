"""Мозг. Два способа достучаться до Claude:
1. Claude API — свой ключ (вводится в настройке, хранится локально в data/).
2. Claude Code CLI — если установлен `claude` и ключа нет (работает по подписке).
"""
import asyncio
import json
import re
import shutil

import anthropic

from .config import PREFS_PATH, PROFILE_PATH
from . import db

DEFAULT_MODEL = "claude-opus-5"
MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]

SYSTEM = """Ты — карьерный агент одного конкретного кандидата. Твоя задача — помочь ему получить оффер.
Ты оцениваешь вакансии с hh.ru и пишешь сопроводительные письма. Пишешь по-русски.

Жёсткие правила для писем:
- Только факты из профиля кандидата. Ничего не выдумывай: ни технологий, ни цифр, ни проектов.
- Если чего-то из требований нет — не ври; можно честно назвать близкий опыт или сказать, что хочет это прокачать.
- 600–1100 символов, 3–4 коротких абзаца. Начало: «Здравствуйте!». Без подписи и контактов (hh их покажет).
- Первая фраза — про главное совпадение с ЭТОЙ вакансией, не про кандидата вообще.
- 1–2 конкретных факта с цифрами, которые бьют в требования вакансии. Без воды, канцелярита и клише
  («командный игрок», «стрессоустойчивый», «с большим интересом ознакомился»).
- Не склеивай проекты: каждый факт должен соответствовать ровно одному пункту профиля, со своими деталями.
- Зарплату, формат работы и график в письме не упоминай, если вакансия прямо не просит их указать.
- Если в описании просят что-то указать в письме (кодовое слово, ответ на вопрос, ссылку) — сделай это.
- Перед ответом перечитай письмо и проверь каждое утверждение по профилю; всё, чего там нет, убери.

Ответ — строго один JSON-объект без markdown-обёртки."""

SCORE_SCHEMA = """{
  "score": 0-100,            // шанс получить приглашение × насколько вакансия ему подходит
  "fit": "strong" | "ok" | "weak" | "no",
  "summary": "одна строка: что за компания и что делать",
  "why": ["почему подходит", "..."],          // 2-3 пункта
  "risks": ["что может не понравиться работодателю / ему", "..."],  // 0-3 пункта
  "salary_note": "оценка вилки относительно его ожиданий, или пусто",
  "resume_hash": "hash резюме, с которым откликаться (только из видимых)",
  "resume_why": "почему это резюме; если лучше подошло бы скрытое — скажи, что его стоит открыть",
  "letter": "текст письма"
}"""


def _read(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else "(не заполнено)"


def _feedback_block() -> str:
    fb = db.feedback_examples()
    parts = []
    if fb["skips"]:
        parts.append("## Вакансии, которые он ОТКЛОНИЛ (учитывай его логику при оценке)\n" + "\n".join(
            f"- {s['title']} / {s['employer']} (ты дал {s['score']}) — его причина: {s['note']}" for s in fb["skips"]))
    if fb["edits"]:
        parts.append("## Как он правит твои письма (подстраивай стиль)\n" + "\n\n".join(
            f"### {e['title']}\nБЫЛО:\n{e['original_letter']}\nСТАЛО:\n{e['letter']}" for e in fb["edits"]))
    return "\n\n".join(parts)


def _resumes_block() -> str:
    rs = db.resumes()
    if not rs:
        return ""
    return "# Его резюме на hh (выбери, с каким откликаться)\n" + "\n\n".join(
        f"## {r['title']} — hash {r['hash']} — {'видимое' if r['visible'] else 'СКРЫТОЕ, откликаться нельзя'}\n{r['text'][:1500]}"
        for r in rs)


def _vacancy_block(v: dict) -> str:
    return (f"## Вакансия {v['id']}\n{v['title']} — {v['employer']}\n"
            f"Зарплата: {v.get('salary') or 'не указана'} | Опыт: {v.get('experience')} | Формат: {v.get('work_format')} | Адрес: {v.get('address')}\n"
            f"Навыки: {v.get('skills')}\n"
            f"{'Письмо обязательно. ' if v.get('letter_required') else ''}{'Есть тест. ' if v.get('has_test') else ''}\n\n"
            f"{v.get('description')}")


def backend() -> dict:
    """Что сейчас используется: api / cli / none."""
    key = db.get_setting("anthropic_api_key")
    if key:
        return {"kind": "api", "model": db.get_setting("model", DEFAULT_MODEL)}
    if shutil.which("claude"):
        return {"kind": "cli", "model": "sonnet"}
    return {"kind": "none", "model": None}


class NotConfigured(RuntimeError):
    pass


async def _claude(prompt: str) -> str:
    b = backend()
    if b["kind"] == "api":
        return await _via_api(prompt, b["model"])
    if b["kind"] == "cli":
        return await _via_cli(prompt)
    raise NotConfigured("Claude не подключён: добавь API-ключ в настройке")


async def _via_api(prompt: str, model: str) -> str:
    client = anthropic.AsyncAnthropic(api_key=db.get_setting("anthropic_api_key"), max_retries=3)
    try:
        # fallbacks="default": если модель откажется по политике, сервер сам доведёт ответ резервной моделью
        resp = await client.beta.messages.create(
            model=model, max_tokens=16000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude API: неверный ключ — проверь в настройке")
    except anthropic.PermissionDeniedError:
        raise RuntimeError("Claude API: у ключа нет доступа к этой модели")
    except anthropic.NotFoundError:
        raise RuntimeError(f"Claude API: модель {model} недоступна — выбери другую в настройке")
    except anthropic.RateLimitError:
        raise RuntimeError("Claude API: лимит запросов, попробую позже")
    except anthropic.APIConnectionError:
        raise RuntimeError("Claude API: нет связи (VPN/прокси?)")
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude отказался обрабатывать эту вакансию")
    return "".join(b.text for b in resp.content if b.type == "text")


async def _via_cli(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        shutil.which("claude") or "claude", "-p", "--setting-sources", "", "--tools", "", "--strict-mcp-config",
        "--no-session-persistence", "--system-prompt", SYSTEM, "--model", "sonnet", "--output-format", "json",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=240)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {err.decode()[:300] or out.decode()[:300]}")
    res = json.loads(out)
    if res.get("is_error"):
        raise RuntimeError(f"claude: {res.get('result')}")
    return res["result"]


async def ping() -> str:
    """Проверка подключения из мастера настройки."""
    return (await _claude('Ответь строго JSON: {"ok": true}'))[:200]


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM не вернул JSON: " + text[:200])
    return json.loads(m.group(0))


async def score(v: dict) -> dict:
    prompt = f"""# Профиль кандидата (факты — только отсюда)
{_read(PROFILE_PATH)}

# Чего он хочет
{_read(PREFS_PATH)}

{_resumes_block()}

{_feedback_block()}

{_vacancy_block(v)}

Оцени вакансию и напиши письмо. Формат ответа:
{SCORE_SCHEMA}"""
    return _parse_json(await _claude(prompt))


async def rewrite(v: dict, comment: str) -> str:
    prompt = f"""# Профиль кандидата (факты — только отсюда)
{_read(PROFILE_PATH)}

{_vacancy_block(v)}

## Текущее письмо
{v.get('letter')}

## Что он хочет поменять
{comment}

Перепиши письмо с учётом его комментария. Ответ: {{"letter": "..."}}"""
    return _parse_json(await _claude(prompt))["letter"]


RESUME_SCHEMA = """{
  "title": "Желаемая должность — как её ищут рекрутеры на hh",
  "salary": "что поставить в зарплату (или пусто, если лучше не указывать) + почему",
  "about": "раздел «О себе», 500–900 символов, без воды",
  "skills": ["ключевые навыки для поля hh, 12–20 штук, самые важные для направления — первыми"],
  "experience": [{"company": "...", "position": "...", "period": "...", "bullets": ["достижение/задача с цифрой", "..."]}],
  "projects": ["свой проект одной строкой — если усиливает направление"],
  "tips": ["что ещё поправить на hh: видимость, фото, тесты, рекомендации и т.п."]
}"""


async def make_resume(direction: str, notes: str) -> dict:
    """Новое резюме под направление — из фактов профиля, в разрезе полей hh."""
    prompt = f"""# Профиль кандидата (факты — только отсюда)
{_read(PROFILE_PATH)}

# Чего он хочет
{_read(PREFS_PATH)}

{_resumes_block()}

# Задача
Собери резюме для hh.ru под направление: «{direction}».
{('Пожелания: ' + notes) if notes else ''}

Правила: только факты из профиля, ничего не выдумывай. Опыт тот же, но формулировки и порядок пунктов
подстрой под направление: сначала то, что важно рекрутеру этого направления. Цифры из профиля сохраняй.
Не копируй существующие резюме — сделай сильнее. Ответ — строго JSON:
{RESUME_SCHEMA}"""
    return _parse_json(await _claude(prompt))


async def draft_profile() -> str:
    """Черновик profile.md из резюме, которые человек уже выложил на hh."""
    rs = db.resumes()
    if not rs:
        raise RuntimeError("сначала подключи hh — резюме нужны как источник фактов")
    text = "\n\n".join(f"## Резюме «{r['title']}»\n{r['text']}" for r in rs)
    prompt = f"""{text}

# Задача
Собери из этих резюме единый профиль кандидата в Markdown — это будет единственный источник фактов для его писем.
Разделы: «Кто я» (2–3 строки), «Опыт» (по местам работы: компания, должность, период, пункты с цифрами),
«Свои проекты», «Навыки» (сгруппируй), «Образование», «Чего НЕ было (не писать в письмах)» — оставь пустым списком с подсказкой.
Только факты из резюме, без выдумок и без контактов. Где факт неточный или противоречит другому резюме — пометь ⚠️.
Ответ — строго JSON: {{"profile": "markdown"}}"""
    return _parse_json(await _claude(prompt))["profile"]
