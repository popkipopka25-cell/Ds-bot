"""Административная панель: статистика, история, общий состав, обновление списков, резервная копия."""
import logging
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

import config
from database import db
from message_manager import lists, render as render_list
from rank_system import RANKS, rank_label
from staff_system import has_any, has_perm, is_owner, is_staff, position_label
from utils import SEP, esc, fmt_dt, ikb, render, send_new

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

HISTORY_PAGE = 12
ACTIONS = {
    "member_add": "➕ Участник добавлен", "member_left": "🚪 Участник выбыл",
    "member_remove": "🗑 Участник удалён", "member_restore": "♻️ Участник восстановлен",
    "roblox_rename": "✏️ Смена Roblox-ника", "rank_change": "📈 Изменение ранга",
    "pvp": "🎮 PvP-проверка", "request_create": "📨 Заявка создана",
    "request_accept": "✅ Заявка принята", "request_release": "↩️ Заявка возвращена",
    "request_cancel": "🚫 Заявка отменена", "staff_add": "👷 Работник назначен",
    "staff_remove": "👷 Работник снят", "staff_position": "✏️ Должность изменена",
    "perms_change": "🔐 Разрешения изменены", "violation": "⚠️ Нарушение",
    "settings": "⚙️ Настройки",
}


def _admin_kb(tg):
    rows = []
    if has_perm(tg, "view_stats"):
        rows.append([("📊 Статистика", "adm:stats")])
    if has_perm(tg, "view_history"):
        rows.append([("📜 История действий", "adm:hist:0")])
    if has_perm(tg, "view_members"):
        rows.append([("👥 Общий состав", "adm:roster")])
    if has_any(tg, "view_profiles", "help_members", "manage_members"):
        rows.append([("🔍 Поиск участника", "search:start")])
    if has_any(tg, "pvp_view", "pvp_accept", "pvp_result", "manage_requests"):
        rows.append([("🎮 Заявки на PvP", "pvp:panel")])
    if has_perm(tg, "manage_staff"):
        rows.append([("👷 Работники", "st:menu")])
    if has_any(tg, "edit_roster", "manage_members"):
        rows.append([("🔄 Обновить списки в группе", "adm:refresh")])
    if is_owner(tg):
        rows.append([("💾 Резервная копия базы", "adm:backup")])
    rows.append([("🖤 Меню", "menu:main")])
    return ikb(rows)


async def show_admin(event) -> None:
    tg = event.from_user.id
    if not is_staff(tg):
        return await render(event, "⛔ Нет доступа.")
    await render(event, f"🖤 <b>ПАНЕЛЬ УПРАВЛЕНИЯ</b>\n{SEP}\nТвоя должность: <b>{position_label(tg)}</b>",
                 _admin_kb(tg))


@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    await state.clear()
    await show_admin(msg)


@router.callback_query(F.data == "adm:menu")
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_admin(cb)


# ------------------------------------------------------------------ статистика
async def show_stats(event) -> None:
    if not has_perm(event.from_user.id, "view_stats"):
        return await render(event, "⛔ Нет доступа.")
    s = db.stats()
    lines = [f"🖤 <b>СТАТИСТИКА DS</b>\n{SEP}",
             f"Всего участников (за всё время): <b>{s['total']}</b>",
             f"Активных: <b>{s['active']}</b>",
             f"Выбывших: <b>{s['inactive']}</b>", ""]
    for code, *_ in RANKS:
        lines.append(f"{rank_label(code)}: <b>{s['ranks'].get(code, 0)}</b>")
    lines += ["", f"Работников: <b>{s['staff']}</b>",
              f"Проверяющих PvP: <b>{s['checkers']}</b>",
              f"Проведено PvP: <b>{s['pvp']}</b>",
              f"Заявок на проверку: <b>{s['requests']}</b> (активных: {s['pending']})"]
    await render(event, "\n".join(lines), ikb([[("🛠 Панель", "adm:menu")]]))


@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    await show_stats(msg)


@router.callback_query(F.data == "adm:stats")
async def cb_stats(cb: CallbackQuery):
    await show_stats(cb)


# ------------------------------------------------------------------ история
async def show_history(event, page: int = 0) -> None:
    if not has_perm(event.from_user.id, "view_history"):
        return await render(event, "⛔ Нет доступа.")
    total = db.history_count()
    pages = max(1, math.ceil(total / HISTORY_PAGE))
    page = min(max(page, 0), pages - 1)
    rows = db.history_page(HISTORY_PAGE, page * HISTORY_PAGE)
    lines = [f"🖤 <b>ИСТОРИЯ ДЕЙСТВИЙ</b>\n{SEP}"]
    for r in rows:
        title = ACTIONS.get(r["action"], r["action"])
        details = f"\n   {esc(r['details'])}" if r["details"] else ""
        lines.append(f"<b>{fmt_dt(r['ts'])}</b> · {esc(r['actor_name'])}\n{title}{details}")
    if not rows:
        lines.append("Записей пока нет.")
    nav = []
    if page > 0:
        nav.append(("◀️", f"adm:hist:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶️", f"adm:hist:{page + 1}"))
    await render(event, "\n\n".join(lines), ikb([nav, [("🛠 Панель", "adm:menu")]]))


@router.message(Command("history"))
async def cmd_history(msg: Message):
    await show_history(msg, 0)


@router.callback_query(F.data.regexp(r"^adm:hist:\d+$"))
async def cb_history(cb: CallbackQuery):
    await show_history(cb, int(cb.data.split(":")[2]))


# ------------------------------------------------------------------ общий состав
async def send_roster(event) -> None:
    if not has_perm(event.from_user.id, "view_members"):
        return await render(event, "⛔ Нет доступа.")
    pages = lists_pages()
    for i, text in enumerate(pages):
        markup = ikb([[("🛠 Панель", "adm:menu")]]) if i == len(pages) - 1 else None
        await send_new(event, text, markup)


def lists_pages() -> list:
    return render_list("all")


@router.message(Command("ranks"))
async def cmd_ranks(msg: Message):
    await send_roster(msg)


@router.callback_query(F.data == "adm:roster")
async def cb_roster(cb: CallbackQuery):
    await send_roster(cb)


# ------------------------------------------------------------------ обновление списков и копия
@router.callback_query(F.data == "adm:refresh")
async def cb_refresh(cb: CallbackQuery):
    if not has_any(cb.from_user.id, "edit_roster", "manage_members"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    await cb.answer("Обновляю…")
    try:
        done = await lists.refresh_all(cb.bot, force=True)
    except Exception:
        log.exception("Ручное обновление списков не удалось")
        return await cb.message.answer("❌ Не удалось обновить списки. Подробности в логе бота.")
    if done:
        await cb.message.answer("✅ Списки в группе обновлены.")
    else:
        await cb.message.answer("⚠️ Группа не привязана. Владелец должен написать /setgroup в группе клана.")


@router.callback_query(F.data == "adm:backup")
async def cb_backup(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("⛔ Только владелец", show_alert=True)
    await cb.answer("Создаю копию…")
    try:
        path = db.backup()
        await cb.message.answer_document(FSInputFile(path), caption="💾 Резервная копия базы DS")
    except (OSError, TelegramAPIError):
        log.exception("Не удалось создать/отправить резервную копию")
        await cb.message.answer("❌ Не удалось создать резервную копию.")
