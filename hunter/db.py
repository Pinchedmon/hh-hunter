import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH, DEFAULT_TITLE_BLACKLIST

# Жизненный цикл вакансии:
# new → (LLM) scored → [ты] approved → sent
#                    ↘ skipped (с причиной — идёт в обучение)
# needs_me — капча / вопросы работодателя / тест: доделываешь руками
STATUSES = ("new", "scoring", "scored", "approved", "sending", "sent", "skipped", "needs_me", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY,
    title TEXT, employer TEXT, salary TEXT, experience TEXT, work_format TEXT, address TEXT,
    skills TEXT, description TEXT,
    letter_required INTEGER DEFAULT 0, has_test INTEGER DEFAULT 0,
    search TEXT,
    status TEXT DEFAULT 'new',
    score INTEGER, verdict TEXT,          -- verdict = JSON от LLM
    letter TEXT,                           -- текущая версия письма
    original_letter TEXT,                  -- что написал LLM (для обучения на твоих правках)
    note TEXT,                             -- причина skip / что требует твоего внимания
    found_at TEXT, updated_at TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS resumes (
    hash TEXT PRIMARY KEY, title TEXT, visible INTEGER, text TEXT, synced_at TEXT
);
CREATE TABLE IF NOT EXISTS resume_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT, notes TEXT, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS log (ts TEXT, level TEXT, msg TEXT);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for key, value in (("searches", []), ("title_blacklist", DEFAULT_TITLE_BLACKLIST)):
            c.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
        cols = {r["name"] for r in c.execute("PRAGMA table_info(vacancies)")}
        if "resume_hash" not in cols:  # миграция: какое резюме отправлять на эту вакансию
            c.execute("ALTER TABLE vacancies ADD COLUMN resume_hash TEXT")


def get_setting(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


def log(msg: str, level: str = "info"):
    print(f"[{level}] {msg}", flush=True)
    with conn() as c:
        c.execute("INSERT INTO log VALUES (?, ?, ?)", (now(), level, msg))


def recent_log(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM log ORDER BY rowid DESC LIMIT ?", (limit,))]


def known_ids() -> set[str]:
    with conn() as c:
        return {r["id"] for r in c.execute("SELECT id FROM vacancies")}


def insert_vacancy(v: dict):
    v = {**v, "found_at": now(), "updated_at": now()}
    cols = ",".join(v)
    with conn() as c:
        c.execute(f"INSERT OR IGNORE INTO vacancies ({cols}) VALUES ({','.join('?' * len(v))})", tuple(v.values()))


def update(vid: str, **fields):
    fields["updated_at"] = now()
    sets = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE vacancies SET {sets} WHERE id=?", (*fields.values(), vid))


def get(vid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
    return row_to_dict(r) if r else None


def by_status(*statuses, order="score DESC, found_at DESC") -> list[dict]:
    q = ",".join("?" * len(statuses))
    with conn() as c:
        rows = c.execute(f"SELECT * FROM vacancies WHERE status IN ({q}) ORDER BY {order}", statuses).fetchall()
    return [row_to_dict(r) for r in rows]


def row_to_dict(r) -> dict:
    d = dict(r)
    d["verdict"] = json.loads(d["verdict"]) if d.get("verdict") else None
    return d


def counts() -> dict:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM vacancies GROUP BY status").fetchall()
        today = c.execute("SELECT COUNT(*) FROM vacancies WHERE status='sent' AND sent_at >= date('now','localtime')").fetchone()[0]
    return {**{r["status"]: r["n"] for r in rows}, "sent_today": today}


def feedback_examples(limit=12) -> dict:
    """Твои 'disagree' — скармливаем LLM, чтобы он подстраивался."""
    with conn() as c:
        skips = c.execute(
            "SELECT title, employer, score, note FROM vacancies WHERE status='skipped' AND note IS NOT NULL AND note != '' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        edits = c.execute(
            "SELECT title, original_letter, letter FROM vacancies WHERE status='sent' AND original_letter IS NOT NULL "
            "AND letter != original_letter ORDER BY sent_at DESC LIMIT 4").fetchall()
    return {"skips": [dict(r) for r in skips], "edits": [dict(r) for r in edits]}


# ---------- резюме ----------

def save_resumes(items: list[dict]):
    with conn() as c:
        c.execute("DELETE FROM resumes")
        c.executemany("INSERT INTO resumes VALUES (?, ?, ?, ?, ?)",
                      [(r["hash"], r["title"], int(r["visible"]), r["text"], now()) for r in items])


def resumes() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM resumes ORDER BY visible DESC, title")]


def default_resume() -> dict | None:
    """Резюме по умолчанию: выбранное в настройках, иначе первое видимое."""
    rs = resumes()
    want = get_setting("default_resume")
    return next((r for r in rs if r["hash"] == want), None) or next((r for r in rs if r["visible"]), None)


def add_draft(direction: str, notes: str, content: dict) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO resume_drafts (direction, notes, content, created_at) VALUES (?, ?, ?, ?)",
                        (direction, notes, json.dumps(content, ensure_ascii=False), now()))
        return cur.lastrowid


def drafts() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM resume_drafts ORDER BY id DESC").fetchall()
    return [{**dict(r), "content": json.loads(r["content"])} for r in rows]
