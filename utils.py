"""Общие вспомогательные функции: время, оформление, клавиатуры."""
import html
import time
from datetime import datetime, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import config

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(config.TIMEZONE)
except Exception:  # нет tzdata или неверное имя пояса
    TZ = timezone.utc

SEP = "━━━━━━━━━━━━━━━━━━"


def now() -> int:
    return int(time.time())


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def fmt_dt(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), TZ).strftime("%d.%m.%Y %H:%M")


def fmt_date(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), TZ).strftime("%d.%m.%Y")


def plural(n: int, forms) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def fmt_remaining(seconds) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} {plural(days, ('день', 'дня', 'дней'))}, {hours} {plural(hours, ('час', 'часа', 'часов'))}"
    if hours:
        return f"{hours} {plural(hours, ('час', 'часа', 'часов'))}, {minutes} {plural(minutes, ('минута', 'минуты', 'минут'))}"
    minutes = max(minutes, 1)
    return f"{minutes} {plural(minutes, ('минута', 'минуты', 'минут'))}"


def cooldown_days_text() -> str:
    days = config.CHECK_COOLDOWN_SECONDS // 86400
    return f"{days} {plural(days, ('день', 'дня', 'дней'))}"


def tg_label(username, full_name=None) -> str:
    if username:
        return "@" + esc(username)
    return esc(full_name) if full_name else "—"


def contact(tg_id, username) -> str:
    if username:
        return "@" + esc(username)
    return f'<a href="tg://user?id={int(tg_id)}">написать</a>'


def actor_name(user) -> str:
    return "@" + user.username if user.username else user.full_name


def ikb(rows) -> InlineKeyboardMarkup:
    """rows: список рядов, ряд — список (текст, callback_data)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


def main_menu_kb(staff: bool) -> InlineKeyboardMarkup:
    rows = [
        [("🖤 Профиль", "profile:show"), ("🎮 PvP-ранг", "rank:show")],
        [("🏆 Топ игроков", "top:0")],
    ]
    if staff:
        rows.append([("🛠 Панель управления", "adm:menu")])
    return ikb(rows)


async def render(event, text: str, markup=None) -> None:
    """Для кнопки редактирует сообщение, для команды отправляет новое."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                await event.message.answer(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


async def send_new(event, text: str, markup=None) -> None:
    """Всегда отправляет новое сообщение (и закрывает «часики» на кнопке)."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(text, reply_markup=markup)
    else:
        await event.answer(text, reply_markup=markup)
