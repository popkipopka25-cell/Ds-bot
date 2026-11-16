"""Настройки DS Bot. Значения берутся из файла .env (см. .env.example)."""
import os
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


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = _int("OWNER_ID")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "chapapshk1").strip().lstrip("@")
GROUP_ID = _int("GROUP_ID")  # необязательно: можно привязать группу командой /setgroup
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv").strip()

DB_PATH = BASE_DIR / os.getenv("DB_FILE", "ds_members.db")
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_KEEP = 14                       # сколько резервных копий хранить
BACKUP_INTERVAL = 24 * 3600            # резервная копия раз в сутки

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
        problems.append("В файле .env не указан BOT_TOKEN (токен от @BotFather).")
    if OWNER_ID is None:
        problems.append("В файле .env не указан OWNER_ID (ваш числовой Telegram ID).")
    if problems:
        raise SystemExit("Ошибка настройки:\n- " + "\n- ".join(problems))
