import os
import platform
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

DB_PATH = DATA / "hunter.db"
# Всё личное живёт только в data/ (в .gitignore и не попадает в архив для друзей)
PROFILE_PATH = DATA / "profile.md"   # факты о тебе — единственный источник правды для писем
PREFS_PATH = DATA / "prefs.md"       # чего хочешь от работы — для оценки вакансий

HH = "https://hh.ru"
CHROME_PROFILE = DATA / "chrome-profile"  # отдельный профиль Chrome, в hh логинишься в нём сам


def find_chrome() -> str | None:
    """Google Chrome / Chromium на macOS, Windows или Linux."""
    candidates = {
        "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Chromium.app/Contents/MacOS/Chromium"],
        "Windows": [os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")],
    }.get(platform.system(), [])
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if found := shutil.which(name):
            return found
    return None


CHROME = find_chrome()
CDP_PORT = 9333

PORT = 5188
LLM_CONCURRENCY = 3
# ---------- предохранитель (guard.py) ----------
# Темп как у живого человека, который активно ищет работу. hh сам режет на 200 откликов в сутки,
# но банят не за число, а за паттерн: пулемётные запросы, ночная активность, игнор капчи.
LIMITS = {
    #        пауза между запросами (сек)   в час   в сутки
    "read": {"gap": (4, 9),                "hour": 120, "day": 600},   # поиск, карточки, резюме
    "send": {"gap": (75, 180),             "hour": 12,  "day": 30},    # отклики
}
SEND_HOURS = (9, 21)        # откликаемся только днём (по часам твоего компьютера)
HUNT_COOLDOWN_MIN = 45      # охота не чаще раза в 45 минут
HUNT_MAX_DETAILS = 40       # за одну охоту читаем не больше 40 новых карточек
SEARCH_MAX_PAGES = 2
PAUSE_BASE_MIN = 20         # капча/429 → пауза 20 мин, дальше ×2 (до 6 ч)
PAUSE_MAX_MIN = 360
DAILY_SEND_LIMIT = LIMITS["send"]["day"]

# Названия, которые отсекаем до LLM, чтобы не жечь токены. Это общий дефолт;
# свой список правишь в настройке (там же можно добавить чужие стеки: «php|1с|java»).
DEFAULT_TITLE_BLACKLIST = (
    r"senior|lead|тимлид|ведущ|старш|главн|архитект|head|director|"
    r"преподав|наставник|ментор|учител|педагог|репетитор|автор курс|"
    r"колл-центр|оператор|продаж|менеджер по продаж"
)

# Регионы hh: 113 — вся Россия (для удалёнки). Город выбирается в настройке.
REMOTE_AREA = "113"
