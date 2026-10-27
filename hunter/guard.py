"""Предохранитель: чтобы hh не принял тебя за бота и не забанил.

Каждый запрос к hh проходит через before(kind):
  1. если стоит пауза (была капча / 429) — ждём или отказываем;
  2. держим случайную паузу от прошлого запроса того же типа (джиттер, без пулемёта);
  3. проверяем бюджет в час и в сутки.
trip() — hh что-то заподозрил: пауза растёт экспоненциально (20 → 40 → 80 мин …).
Счётчики живут в SQLite, поэтому переживают перезапуск.
"""
import asyncio
import random
from datetime import datetime, timedelta

from .config import LIMITS, PAUSE_BASE_MIN, PAUSE_MAX_MIN, SEND_HOURS
from . import db


class Blocked(Exception):
    """Сейчас к hh нельзя: пауза, лимит или ночь. retry_at — когда можно снова."""
    def __init__(self, reason: str, retry_at: datetime):
        super().__init__(reason)
        self.reason, self.retry_at = reason, retry_at


def _init():
    with db.conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hh_requests (ts TEXT, kind TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS hh_requests_ts ON hh_requests (ts)")


def _count(kind: str, since: datetime) -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM hh_requests WHERE kind=? AND ts>=?",
                         (kind, since.isoformat(timespec="seconds"))).fetchone()[0]


def _oldest_in_window(kind: str, since: datetime) -> datetime:
    with db.conn() as c:
        ts = c.execute("SELECT MIN(ts) FROM hh_requests WHERE kind=? AND ts>=?",
                       (kind, since.isoformat(timespec="seconds"))).fetchone()[0]
    return datetime.fromisoformat(ts)


def _last(kind: str) -> datetime | None:
    with db.conn() as c:
        ts = c.execute("SELECT MAX(ts) FROM hh_requests WHERE kind=?", (kind,)).fetchone()[0]
    return datetime.fromisoformat(ts) if ts else None


def paused_until() -> datetime | None:
    v = db.get_setting("pause_until")
    t = datetime.fromisoformat(v) if v else None
    return t if t and t > datetime.now() else None


def check(kind: str) -> None:
    """Можно ли сейчас сделать запрос. Без ожидания — бросает Blocked."""
    now = datetime.now()
    if p := paused_until():
        raise Blocked(f"пауза после сигнала от hh: {db.get_setting('pause_reason', '')}", p)
    lim = LIMITS[kind]
    if kind == "send" and not (SEND_HOURS[0] <= now.hour < SEND_HOURS[1]):
        start = now.replace(hour=SEND_HOURS[0], minute=random.randint(0, 25), second=0)
        raise Blocked("ночью не откликаемся", start if now.hour < SEND_HOURS[0] else start + timedelta(days=1))
    for window, key in ((timedelta(hours=1), "hour"), (timedelta(days=1), "day")):
        since = now - window
        if _count(kind, since) >= lim[key]:
            raise Blocked(f"лимит {lim[key]} {kind} за {'час' if key == 'hour' else 'сутки'}",
                          _oldest_in_window(kind, since) + window + timedelta(seconds=30))


async def before(kind: str, max_wait: float = 15 * 60) -> None:
    """Вызывать перед КАЖДЫМ запросом к hh. Ждёт джиттер; если ждать дольше max_wait — Blocked."""
    check(kind)
    lo, hi = LIMITS[kind]["gap"]
    if last := _last(kind):
        wait = random.uniform(lo, hi) - (datetime.now() - last).total_seconds()
        if wait > max_wait:
            raise Blocked("слишком частые запросы", datetime.now() + timedelta(seconds=wait))
        if wait > 0:
            await asyncio.sleep(wait)
    with db.conn() as c:
        c.execute("INSERT INTO hh_requests VALUES (?, ?)", (datetime.now().isoformat(), kind))


def trip(reason: str) -> datetime:
    """hh показал капчу / 429 / 403 — останавливаем ВСЁ, пауза растёт с каждым разом."""
    last_trip = db.get_setting("last_trip")
    strikes = db.get_setting("strikes", 0)
    if last_trip and datetime.now() - datetime.fromisoformat(last_trip) > timedelta(hours=24):
        strikes = 0  # сутки без проблем — прощаем
    strikes += 1
    minutes = min(PAUSE_BASE_MIN * 2 ** (strikes - 1), PAUSE_MAX_MIN)
    until = datetime.now() + timedelta(minutes=minutes)
    db.set_setting("strikes", strikes)
    db.set_setting("last_trip", db.now())
    db.set_setting("pause_until", until.isoformat(timespec="seconds"))
    db.set_setting("pause_reason", reason)
    db.log(f"🛑 hh: {reason}. Пауза {minutes} мин (до {until:%H:%M}), сигнал №{strikes} за сутки", "warn")
    return until


def unpause():
    db.set_setting("pause_until", None)
    db.log("пауза снята вручную")


def status() -> dict:
    now = datetime.now()
    out = {"paused_until": (p.isoformat(timespec="minutes") if (p := paused_until()) else None),
           "pause_reason": db.get_setting("pause_reason", "") if paused_until() else "",
           "strikes": db.get_setting("strikes", 0), "send_hours": SEND_HOURS}
    for kind, lim in LIMITS.items():
        out[kind] = {"hour": _count(kind, now - timedelta(hours=1)), "hour_max": lim["hour"],
                     "day": _count(kind, now - timedelta(days=1)), "day_max": lim["day"]}
    return out


_init()
