"""Настройки DS Bot. Значения берутся из переменных окружения (Render → Environment)
или, при локальном запуске, из файла .env (см. .env.example)."""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE_DIR / ".env")


def _int(name: str):
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = _int("OWNER_ID")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "chapapshk1").strip().lstrip("@")
GROUP_ID = _int("GROUP_ID")  # необязательно: можно привязать группу командой /setgroup
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv").strip()

# ---------------------------------------------------------------- база данных (Neon PostgreSQL)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip().strip('"').strip("'")

# ---------------------------------------------------------------- веб-сервер для Render / UptimeRobot
PORT = _int("PORT") or 10000          # Render сам задаёт PORT
WEB_HOST = "0.0.0.0"

# ---------------------------------------------------------------- резервные копии (JSON-выгрузка)
# Диск Render временный, поэтому настоящее хранилище — Neon, а копии отправляются владельцу в Telegram.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "").strip() or (Path(tempfile.gettempdir()) / "ds_backups"))
BACKUP_KEEP = 14                       # сколько копий держать во временной папке
BACKUP_INTERVAL = 24 * 3600            # копия раз в сутки
BACKUP_SEND_TO_OWNER = _bool("BACKUP_SEND_TO_OWNER", True)  # присылать копию владельцу в личку

CHECK_COOLDOWN_SECONDS = 3 * 24 * 3600  # пауза между PvP-проверками: 3 дня
SYNC_INTERVAL_SECONDS = 6 * 3600        # сверка состава с Telegram и Roblox
RECONCILE_INTERVAL = 15 * 60            # сверка опубликованных списков
REFRESH_DEBOUNCE = 2.0                  # собираем несколько изменений в одно обновление

# ---------------------------------------------------------------- должности и права
POSITIONS = {
    "chief_admin": "Главный администратор",
    "admin": "Администратор",
    "moderator": "Модератор",
    "pvp_checker": "Проверяющий PvP",
    "helper": "Помощник",
    "roster_editor": "Редактор состава",
}
POSITION_ORDER = list(POSITIONS)

PERMISSIONS = {
    "manage_members": "Управление участниками",
    "delete_members": "Удаление участников",
    "view_stats": "Просмотр статистики",
    "manage_requests": "Управление заявками",
    "manage_staff": "Управление работниками",
    "handle_violations": "Работа с нарушениями",
    "view_members": "Просмотр состава",
    "view_profiles": "Просмотр профилей",
    "help_members": "Помощь участникам",
    "pvp_view": "Просмотр заявок на PvP",
    "pvp_accept": "Принятие заявок на PvP",
    "pvp_result": "Внесение результатов PvP",
    "edit_roster": "Обновление списков состава",
    "view_history": "Просмотр истории действий",
}
PERMISSION_ORDER = list(PERMISSIONS)

DEFAULT_PERMS = {
    "chief_admin": {"manage_members", "manage_staff", "delete_members", "view_stats",
                    "manage_requests", "view_members", "view_profiles", "view_history",
                    "edit_roster"},
    "admin": {"manage_members", "delete_members", "view_stats", "view_members",
              "view_profiles", "view_history"},
    "moderator": {"handle_violations", "view_members", "view_profiles"},
    "pvp_checker": {"pvp_view", "pvp_accept", "pvp_result"},
    "helper": {"view_members", "view_profiles", "help_members"},
    "roster_editor": {"edit_roster", "view_members"},
}


def validate() -> None:
    problems = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        problems.append("Не указан BOT_TOKEN (токен от @BotFather).")
    if OWNER_ID is None:
        problems.append("Не указан OWNER_ID (ваш числовой Telegram ID).")
    if not DATABASE_URL:
        problems.append("Не указан DATABASE_URL (строка подключения Neon PostgreSQL).")
    elif not DATABASE_URL.startswith(("postgresql://", "postgres://")):
        problems.append("DATABASE_URL должен начинаться с postgresql:// "
                        "(вставляйте только саму строку подключения, без слова psql и кавычек).")
    if problems:
        raise SystemExit("Ошибка настройки:\n- " + "\n- ".join(problems))
