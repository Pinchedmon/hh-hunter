import asyncio
import json
import random
import re
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from datetime import datetime, timedelta

from . import db, guard, hh, llm
from .config import (CHROME, DAILY_SEND_LIMIT, HUNT_COOLDOWN_MIN, HUNT_MAX_DETAILS, LLM_CONCURRENCY, PORT,
                     PREFS_PATH, PROFILE_PATH, REMOTE_AREA, ROOT)

send_wakeup = asyncio.Event()
score_wakeup = asyncio.Event()
hunt_running = False


# ---------- фоновые работники ----------

async def score_one(vid: str, sem: asyncio.Semaphore):
    async with sem:
        v = db.get(vid)
        db.update(vid, status="scoring")
        try:
            r = await llm.score(v)
            visible = {x["hash"] for x in db.resumes() if x["visible"]}
            pick = r.get("resume_hash") if r.get("resume_hash") in visible else (db.default_resume() or {}).get("hash")
            db.update(vid, status="scored", score=int(r.get("score", 0)), verdict=json.dumps(r, ensure_ascii=False),
                      letter=r.get("letter", ""), original_letter=r.get("letter", ""), resume_hash=pick)
            db.log(f"оценил {v['title']} — {v['employer']}: {r.get('score')}")
        except Exception as e:
            db.update(vid, status="new", note=f"LLM: {e}")
            db.log(f"LLM упал на {vid}: {e}", "error")
            await asyncio.sleep(10)


async def scorer():
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    while True:
        pending = [v["id"] for v in db.by_status("new", order="found_at")]
        if pending and llm.backend()["kind"] != "none":
            await asyncio.gather(*(score_one(vid, sem) for vid in pending))
            continue
        score_wakeup.clear()
        await score_wakeup.wait()


async def sender():
    """Берёт одобренные по очереди. Темп, лимиты, ночь и паузы решает guard — тут только ждём."""
    while True:
        queue = db.by_status("approved", order="updated_at")
        if not queue:
            send_wakeup.clear()
            await send_wakeup.wait()
            continue
        try:
            await guard.before("send", max_wait=4 * 60)
        except guard.Blocked as b:
            note = f"ждёт: {b.reason} — до {b.retry_at:%d.%m %H:%M}"
            for v in queue:
                db.update(v["id"], note=note)
            send_wakeup.clear()
            try:  # спим до разрешения, но просыпаемся, если что-то поменялось (снята пауза, новый approve)
                await asyncio.wait_for(send_wakeup.wait(), timeout=max(30, (b.retry_at - datetime.now()).total_seconds()))
            except asyncio.TimeoutError:
                pass
            continue
        v = queue[0]
        db.update(v["id"], status="sending", note="")
        try:
            resume = next((r for r in db.resumes() if r["hash"] == v.get("resume_hash")), None) or db.default_resume()
            st, note = await hh.apply(v["id"], v["letter"], resume)
        except guard.Blocked as b:
            st, note = "approved", f"пауза до {b.retry_at:%H:%M}: {b.reason}"
        except Exception as e:
            st, note = "error", str(e)[:300]
        db.update(v["id"], status=st, note=note, **({"sent_at": db.now()} if st == "sent" else {}))
        db.log(f"отклик {v['title']} — {v['employer']}: {st} {note}", "info" if st == "sent" else "warn")


async def hunt():
    global hunt_running
    if hunt_running:
        return
    last = db.get_setting("last_hunt")
    if last and datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=HUNT_COOLDOWN_MIN):
        nxt = datetime.fromisoformat(last) + timedelta(minutes=HUNT_COOLDOWN_MIN)
        db.log(f"охота отдыхает до {nxt:%H:%M} — hh не любит частый поиск", "warn")
        return
    hunt_running = True
    db.set_setting("last_hunt", db.now())
    try:
        known = db.known_ids()
        bl = re.compile(db.get_setting("title_blacklist") or "(?!x)x", re.I)
        fresh: dict[str, dict] = {}
        for s in db.get_setting("searches", []):
            items = await hh.search(s)
            if len(fresh) >= HUNT_MAX_DETAILS:
                break
            new = [i for i in items if i["id"] not in known and i["id"] not in fresh and not bl.search(i["title"] or "")]
            db.log(f"поиск «{s['name']}»: {len(items)} всего, {len(new)} новых")
            for i in new:
                fresh[i["id"]] = {**i, "search": s["name"]}
        for vid, i in list(fresh.items())[:HUNT_MAX_DETAILS]:
            try:
                d = await hh.vacancy(vid)
            except guard.Blocked:
                raise
            except Exception as e:
                db.log(f"не прочитал вакансию {vid}: {e}", "error")
                continue
            if d.pop("archived", False) or not d["title"]:
                continue
            db.insert_vacancy({"id": vid, "search": i["search"], **d})
            score_wakeup.set()
        db.log(f"охота завершена: {min(len(fresh), HUNT_MAX_DETAILS)} новых вакансий на оценку")
    except guard.Blocked as b:
        db.log(f"охота остановлена: {b.reason} (до {b.retry_at:%H:%M})", "warn")
    finally:
        hunt_running = False


@asynccontextmanager
async def lifespan(app):
    db.init()
    with db.conn() as c:  # после рестарта возвращаем зависшее
        c.execute("UPDATE vacancies SET status='new' WHERE status='scoring'")
        c.execute("UPDATE vacancies SET status='approved' WHERE status='sending'")
    tasks = [asyncio.create_task(scorer()), asyncio.create_task(sender())]
    if not db.resumes():
        tasks.append(asyncio.create_task(sync_resumes()))
    score_wakeup.set()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(guard.Blocked)
async def blocked_handler(request, b: guard.Blocked):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": f"{b.reason} — до {b.retry_at:%H:%M}"}, status_code=429)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    return {"counts": db.counts(), "log": db.recent_log(30), "hunting": hunt_running,
            "searches": db.get_setting("searches", []), "setup_done": bool(db.get_setting("setup_done")),
            "title_blacklist": db.get_setting("title_blacklist", ""), "llm": llm.backend(),
            "daily_limit": DAILY_SEND_LIMIT, "guard": guard.status(),
            "next_hunt": _next_hunt()}


def _next_hunt():
    last = db.get_setting("last_hunt")
    if not last:
        return None
    nxt = datetime.fromisoformat(last) + timedelta(minutes=HUNT_COOLDOWN_MIN)
    return nxt.isoformat(timespec="minutes") if nxt > datetime.now() else None


@app.post("/api/unpause")
def unpause():
    guard.unpause()
    send_wakeup.set()
    return {"ok": True}


TABS = {
    "queue": ("scored",),
    "needs": ("needs_me", "error"),
    "outbox": ("approved", "sending"),
    "sent": ("sent",),
    "skipped": ("skipped",),
    "pending": ("new", "scoring"),
}


@app.get("/api/list/{tab}")
def list_tab(tab: str):
    order = "sent_at DESC" if tab == "sent" else "updated_at DESC" if tab in ("skipped", "needs") else "score DESC, found_at DESC"
    return db.by_status(*TABS[tab], order=order)


@app.post("/api/hunt")
async def start_hunt():
    asyncio.create_task(hunt())
    return {"ok": True}


_browser_cache: dict = {}


@app.get("/api/browser")
async def browser_status():
    """Не дёргаем hh на каждое открытие страницы — кэш на 15 минут."""
    if _browser_cache.get("at") and datetime.now() - _browser_cache["at"] < timedelta(minutes=15):
        return _browser_cache["data"]
    try:
        data = await hh.status()
    except guard.Blocked as b:
        return {"logged": None, "resumes": [], "blocked": b.reason}
    _browser_cache.update(at=datetime.now(), data=data)
    return data


def _need(vid):
    v = db.get(vid)
    if not v:
        raise HTTPException(404)
    return v


@app.post("/api/v/{vid}/approve")
def approve(vid: str, letter: str = Body(..., embed=True), resume_hash: str = Body("", embed=True)):
    v = _need(vid)
    db.update(vid, status="approved", letter=letter, note="", resume_hash=resume_hash or v.get("resume_hash"))
    send_wakeup.set()
    return {"ok": True}


@app.post("/api/v/{vid}/skip")
def skip(vid: str, reason: str = Body("", embed=True)):
    _need(vid)
    db.update(vid, status="skipped", note=reason)
    return {"ok": True}


@app.post("/api/v/{vid}/save")
def save(vid: str, letter: str = Body(..., embed=True)):
    _need(vid)
    db.update(vid, letter=letter)
    return {"ok": True}


@app.post("/api/v/{vid}/rewrite")
async def rewrite(vid: str, comment: str = Body(..., embed=True), letter: str = Body("", embed=True)):
    v = _need(vid)
    if letter:
        v["letter"] = letter
    new = await llm.rewrite(v, comment)
    db.update(vid, letter=new)
    return {"letter": new}


@app.post("/api/v/{vid}/rescore")
def rescore(vid: str):
    _need(vid)
    db.update(vid, status="new")
    score_wakeup.set()
    return {"ok": True}


@app.post("/api/v/{vid}/mark_sent")
def mark_sent(vid: str):
    _need(vid)
    db.update(vid, status="sent", sent_at=db.now(), note="отправлено вручную")
    return {"ok": True}


@app.post("/api/add")
async def add(url: str = Body(..., embed=True)):
    m = re.search(r"(\d{6,})", url)
    if not m:
        raise HTTPException(400, "не вижу id вакансии")
    vid = m.group(1)
    d = await hh.vacancy(vid)
    d.pop("archived", None)
    db.insert_vacancy({"id": vid, "search": "вручную", **d})
    db.update(vid, status="new")
    score_wakeup.set()
    return {"id": vid}


@app.post("/api/searches")
def save_searches(searches: list = Body(..., embed=True)):
    db.set_setting("searches", searches)
    return {"ok": True}


async def sync_resumes():
    try:
        rs = await hh.list_resumes()
        db.save_resumes(rs)
        hidden = [r["title"] for r in rs if not r["visible"]]
        db.log(f"резюме синхронизированы: {len(rs)} шт." + (f" Скрыты: {', '.join(hidden)}" if hidden else ""))
    except Exception as e:
        db.log(f"не смог прочитать резюме: {e}", "error")


@app.get("/api/resumes")
def get_resumes():
    d = db.default_resume()
    return {"resumes": [{k: r[k] for k in ("hash", "title", "visible", "synced_at")} for r in db.resumes()],
            "default": d["hash"] if d else None}


@app.post("/api/resumes/sync")
async def resumes_sync():
    await sync_resumes()
    return get_resumes()


@app.post("/api/default_resume")
def set_default_resume(hash: str = Body(..., embed=True)):
    db.set_setting("default_resume", hash)
    return {"ok": True}


@app.get("/api/resume_drafts")
def get_drafts():
    return db.drafts()


@app.post("/api/resume_drafts")
async def new_draft(direction: str = Body(..., embed=True), notes: str = Body("", embed=True)):
    content = await llm.make_resume(direction, notes)
    return {"id": db.add_draft(direction, notes, content), "content": content}


@app.post("/api/title_blacklist")
def save_blacklist(value: str = Body(..., embed=True)):
    try:
        re.compile(value)
    except re.error as e:
        raise HTTPException(400, f"кривое регулярное выражение: {e}")
    db.set_setting("title_blacklist", value)
    return {"ok": True}


# ---------- мастер настройки (для нового человека) ----------

@app.get("/api/setup")
def setup_state():
    key = db.get_setting("anthropic_api_key") or ""
    return {
        "llm": llm.backend(), "models": llm.MODELS, "key_hint": f"…{key[-4:]}" if key else "",
        "chrome": bool(CHROME), "logged": (_browser_cache.get("data") or {}).get("logged"),
        "resumes": len(db.resumes()), "profile": PROFILE_PATH.exists() and PROFILE_PATH.stat().st_size > 50,
        "prefs": PREFS_PATH.exists(), "searches": len(db.get_setting("searches", [])),
        "city": db.get_setting("city"), "prefs_form": db.get_setting("prefs_form"),
        "setup_done": bool(db.get_setting("setup_done")),
    }


@app.post("/api/setup/llm")
async def setup_llm(api_key: str = Body("", embed=True), model: str = Body("", embed=True),
                    clear: bool = Body(False, embed=True)):
    prev_key, prev_model = db.get_setting("anthropic_api_key"), db.get_setting("model")
    if clear:
        db.set_setting("anthropic_api_key", None)
    elif api_key.strip():
        db.set_setting("anthropic_api_key", api_key.strip())
    if model in llm.MODELS:
        db.set_setting("model", model)
    try:
        await llm.ping()
    except Exception as e:  # неработающий ключ не сохраняем — откатываем
        db.set_setting("anthropic_api_key", prev_key)
        db.set_setting("model", prev_model)
        raise HTTPException(400, str(e))
    score_wakeup.set()
    return {"ok": True, "llm": llm.backend()}


@app.post("/api/setup/hh/open")
async def setup_hh_open():
    try:
        await hh.open_login()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/setup/hh/check")
async def setup_hh_check():
    _browser_cache.clear()
    data = await browser_status()
    if data.get("logged"):
        await sync_resumes()
    return {**data, "resumes": len(db.resumes())}


@app.post("/api/setup/profile/draft")
async def setup_profile_draft():
    try:
        return {"text": await llm.draft_profile()}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/areas")
async def areas(q: str):
    return await hh.suggest_areas(q) if len(q) >= 2 else []


EXPERIENCE = {"none": "noExperience", "junior": "between1And3", "middle": "between3And6", "any": ""}


@app.post("/api/setup/prefs")
def setup_prefs(form: dict = Body(..., embed=True)):
    """Анкета «что ищем» → prefs.md (для оценки) + набор поисков hh."""
    city = form.get("city") or {}
    formats = form.get("formats") or []
    keywords = [k.strip() for k in (form.get("keywords") or "").split(",") if k.strip()][:5]
    if not city.get("id") or not keywords:
        raise HTTPException(400, "нужны город и хотя бы одно ключевое слово")
    fmt_names = {"remote": "удалёнка", "hybrid": "гибрид", "office": "офис"}
    PREFS_PATH.write_text(f"""# Что ищу

- Город: {city['name']}. Формат: {', '.join(fmt_names[f] for f in formats) or 'любой'}. Переезд: {'готов' if form.get('relocate') else 'нет'}.
- Деньги: комфортно от {form.get('salary_comfort') or '—'} ₽ на руки, рассматриваю от {form.get('salary_min') or '—'} ₽.
- Уровень: {form.get('grade_text') or form.get('grade')}.
- Направления по приоритету: {form.get('directions') or ', '.join(keywords)}.
- Не хочу: {form.get('avoid') or '—'}.
- Вакансии, где требуют заметно больше опыта, — можно, если стек совпадает 1-в-1, но оценка ниже.
""", encoding="utf-8")
    exp = EXPERIENCE.get(form.get("grade") or "any", "")
    searches = []
    for k in keywords:
        if "office" in formats or "hybrid" in formats or not formats:
            searches.append({"name": f"{k} · {city['name']}", "text": k, "search_field": "name",
                             "area": city["id"], "experience": exp})
        if "remote" in formats:
            searches.append({"name": f"{k} · удалёнка", "text": k, "search_field": "name",
                             "area": REMOTE_AREA, "work_format": "REMOTE", "experience": exp})
    db.set_setting("searches", searches)
    db.set_setting("city", city)
    db.set_setting("prefs_form", form)
    db.set_setting("setup_done", True)
    return {"ok": True, "searches": searches}


DOCS = {"profile": PROFILE_PATH, "prefs": PREFS_PATH}


@app.get("/api/doc/{name}")
def get_doc(name: str):
    p = DOCS[name]
    return {"text": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.post("/api/doc/{name}")
def put_doc(name: str, text: str = Body(..., embed=True)):
    DOCS[name].write_text(text, encoding="utf-8")
    return {"ok": True}


def main():
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
