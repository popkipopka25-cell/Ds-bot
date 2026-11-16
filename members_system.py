"""Участники: регистрация, профиль, топ, поиск, учёт входа/выхода из Telegram-группы."""
import asyncio
import logging
import math
import sqlite3

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import (JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command,
                             CommandObject, CommandStart)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

import config
import roblox
from database import SYSTEM, db
from message_manager import lists
from rank_system import rank_label
from staff_system import (can_manage_target, has_any, has_perm, is_owner, is_staff, notify,
                          position_label)
from utils import (SEP, actor_name, cooldown_days_text, esc, fmt_date, fmt_dt, fmt_remaining, ikb,
                   main_menu_kb, now, render, send_new, tg_label)

log = logging.getLogger(__name__)

router = Router(name="members")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
group_router = Router(name="group")

TOP_PAGE = 10


class Reg(StatesGroup):
    nick = State()


class Search(StatesGroup):
    query = State()


class Violation(StatesGroup):
    text = State()


WELCOME = ("🖤 Добро пожаловать в DS!\n\n"
           "Твой Roblox-ник успешно проверен.\n"
           "Ты добавлен в состав клана.\n\n"
           "Теперь ты можешь просматривать свой профиль и PvP-ранг через бота")


def can_view_cards(tg_id) -> bool:
    return has_any(tg_id, "view_profiles", "help_members", "manage_members")


# ------------------------------------------------------------------ старт и меню
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    u = db.get_user_by_tg(msg.from_user.id)
    staff = is_staff(msg.from_user.id)
    if u and u["status"] == "active":
        return await msg.answer(
            f"🖤 <b>DS</b>\n{SEP}\nС возвращением, <b>{esc(u['roblox_name'])}</b>!\nВыбери действие:",
            reply_markup=main_menu_kb(staff))
    if u and u["status"] == "removed":
        return await msg.answer("⛔ Твоя запись удалена администрацией клана. Обратись к администратору.",
                                reply_markup=main_menu_kb(staff) if staff else None)
    intro = "Ты выбыл из состава клана. Чтобы вернуться, " if u else "Привет! Это бот клана <b>DS</b>. Чтобы вступить, "
    await state.set_state(Reg.nick)
    await msg.answer(f"🖤 <b>DS</b>\n{SEP}\n{intro}отправь свой <b>Roblox-ник</b>.\n"
                     f"Ник должен заканчиваться на <b>DS</b> (например, ChapaDS).",
                     reply_markup=main_menu_kb(True) if staff else None)


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery):
    await render(cb, f"🖤 <b>DS</b>\n{SEP}\nВыбери действие:", main_menu_kb(is_staff(cb.from_user.id)))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Действие отменено.")


@router.message(Command("help"))
async def cmd_help(msg: Message):
    tg = msg.from_user.id
    lines = ["/start — запуск и регистрация", "/profile — мой профиль", "/rank — мой PvP-ранг",
             "/rankrequest — заявка на PvP-проверку", "/ranklist — таблица рангов",
             "/top — топ игроков"]
    if is_staff(tg):
        lines.append("/admin — панель управления")
        if has_perm(tg, "view_stats"):
            lines.append("/stats — статистика")
        if has_perm(tg, "view_history"):
            lines.append("/history — история действий")
        if has_perm(tg, "view_members"):
            lines.append("/ranks — общий состав")
        if has_perm(tg, "manage_staff"):
            lines.append("/staff — работники")
        if can_view_cards(tg):
            lines.append("/search — поиск участника")
        if has_any(tg, "pvp_view", "pvp_accept", "pvp_result", "manage_requests"):
            lines.append("/pvp — заявки на PvP")
    await msg.answer(f"🖤 <b>ПОМОЩЬ</b>\n{SEP}\n" + "\n".join(lines))


# ------------------------------------------------------------------ регистрация
@router.message(Reg.nick, F.text, ~F.text.startswith("/"))
async def reg_nick(msg: Message, state: FSMContext, bot: Bot):
    nick = msg.text.strip()
    tg = msg.from_user
    if not roblox.valid_nick_format(nick):
        return await msg.answer("❌ Ник должен содержать 3–20 символов: латиница, цифры и «_». "
                                "Отправь ник ещё раз.")
    await msg.answer("🔎 Проверяю аккаунт в Roblox…")
    try:
        info = await roblox.get_user_by_name(nick)
    except roblox.RobloxError:
        return await msg.answer("⚠️ Roblox сейчас недоступен. Попробуй через пару минут — просто отправь ник ещё раз.")
    if info is None:
        return await msg.answer("❌ Аккаунт с таким ником не найден в Roblox. Проверь написание и отправь ещё раз.")
    if not roblox.ends_with_ds(info.name):
        return await msg.answer(f"❌ Ник <b>{esc(info.name)}</b> не подходит: он должен заканчиваться на <b>DS</b> "
                                f"(например, ChapaDS). Измени ник в Roblox и отправь ещё раз.")

    group_id = db.get_group_id()
    if group_id:
        try:
            member = await bot.get_chat_member(group_id, tg.id)
            if member.status in ("left", "kicked"):
                return await msg.answer("Сначала вступи в Telegram-группу клана, затем отправь ник ещё раз.")
        except TelegramAPIError as exc:
            log.warning("Не удалось проверить членство в группе: %s", exc)

    by_roblox = db.get_user_by_roblox(info.id)
    by_tg = db.get_user_by_tg(tg.id)
    if by_roblox and by_roblox["tg_id"] != tg.id:
        return await msg.answer("❌ Этот Roblox-аккаунт уже зарегистрирован другим пользователем.")
    if by_tg and by_tg["roblox_id"] != info.id:
        return await msg.answer(f"❌ Ты уже зарегистрирован с ником <b>{esc(by_tg['roblox_name'])}</b>. "
                                f"Для смены аккаунта обратись к администрации.")
    if by_tg and by_tg["status"] == "active":
        await state.clear()
        return await msg.answer("ℹ️ Ты уже в составе клана.", reply_markup=main_menu_kb(is_staff(tg.id)))
    if by_tg and by_tg["status"] == "removed":
        await state.clear()
        return await msg.answer("⛔ Твоя запись удалена администрацией. Обратись к администратору.")

    try:
        if by_tg:  # восстановление выбывшего участника: ранг и история сохраняются
            db.update_roblox_name(by_tg["id"], info.name, "при повторной регистрации")
            db.set_member_status(by_tg["id"], "active", tg.id, actor_name(tg), "Повторная регистрация")
            event = f"♻️ Восстановлен участник <b>{esc(info.name)}</b> ({tg_label(tg.username, tg.full_name)})."
        else:
            db.create_user(tg.id, tg.username, tg.full_name, info.id, info.name, actor_name(tg))
            event = f"➕ Новый участник: <b>{esc(info.name)}</b> ({tg_label(tg.username, tg.full_name)})."
    except sqlite3.IntegrityError:
        return await msg.answer("❌ Этот аккаунт уже зарегистрирован.")

    await state.clear()
    lists.request_refresh()
    await msg.answer(WELCOME, reply_markup=main_menu_kb(is_staff(tg.id)))
    await notify(bot, "manage_members", event)


# ------------------------------------------------------------------ профиль
def profile_text(u, staff_view: bool = False) -> str:
    position = position_label(u["tg_id"]) or "Участник"
    nxt = u["next_check_at"]
    if nxt and nxt > now():
        nxt_txt = f"{fmt_dt(nxt)} (через {fmt_remaining(nxt - now())})"
    else:
        nxt_txt = "доступна сейчас"
    lines = [
        f"🖤 <b>ПРОФИЛЬ DS</b>\n{SEP}",
        f"Roblox: <b>{esc(u['roblox_name'])}</b>",
        f"Telegram: {tg_label(u['username'], u['full_name'])}",
        f"PvP-ранг: <b>{rank_label(u['rank_code'])}</b>",
        f"Должность: {position}",
        f"Дата вступления: {fmt_date(u['joined_at'])}",
        f"Последний PvP: {fmt_date(u['last_pvp_at'])}",
        f"Следующая проверка: {nxt_txt}",
        f"Количество PvP-проверок: {u['pvp_count']}",
    ]
    if staff_view:
        status = {"active": "в составе", "left": "вышел из группы", "removed": "исключён"}[u["status"]]
        lines.insert(2, f"Статус: {status}")
        lines.insert(3, f"Roblox ID: {u['roblox_id']} · Telegram ID: {u['tg_id']}")
    matches = db.user_matches(u["id"], 10)
    lines.append("")
    lines.append("<b>История изменения рангов:</b>")
    if matches:
        for m in matches:
            arrow = f"{m['old_rank']} → {m['new_rank']}" if m["old_rank"] != m["new_rank"] \
                else f"{m['new_rank']} (без изменений)"
            lines.append(f"• {fmt_date(m['created_at'])}: {arrow} ({m['user_wins']} побед)")
    else:
        lines.append("• Проверок ещё не было")
    return "\n".join(lines)


async def show_profile(event) -> None:
    u = db.get_user_by_tg(event.from_user.id)
    if not u or u["status"] != "active":
        return await render(event, "Ты не в составе клана. Нажми /start, чтобы зарегистрироваться.")
    await render(event, profile_text(u), ikb([[("🎮 PvP-ранг", "rank:show"), ("🖤 Меню", "menu:main")]]))


@router.message(Command("profile"))
async def cmd_profile(msg: Message):
    await show_profile(msg)


@router.callback_query(F.data == "profile:show")
async def cb_profile(cb: CallbackQuery):
    await show_profile(cb)


# ------------------------------------------------------------------ топ
async def show_top(event, page: int = 0) -> None:
    total = db.count_active()
    pages = max(1, math.ceil(total / TOP_PAGE))
    page = min(max(page, 0), pages - 1)
    rows = db.top_page(TOP_PAGE, page * TOP_PAGE)
    if not rows:
        return await render(event, f"🖤 <b>ТОП ИГРОКОВ</b>\n{SEP}\nПока в составе никого нет.")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, r in enumerate(rows, page * TOP_PAGE + 1):
        wins = f" · победы в последней проверке: {r['last_wins']}" if r["last_wins"] is not None else ""
        lines.append(f"{medals.get(i, str(i) + '.')} {r['rank_code']} · <b>{esc(r['roblox_name'])}</b>{wins}")
    nav = []
    if page > 0:
        nav.append(("◀️", f"top:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶️", f"top:{page + 1}"))
    note = "<i>Порядок: ранг → победы в последней проверке → число проверок → дата вступления.</i>"
    await render(event, f"🖤 <b>ТОП ИГРОКОВ</b>\n{SEP}\n" + "\n".join(lines) + f"\n\n{note}",
                 ikb([nav, [("🖤 Меню", "menu:main")]]))


@router.message(Command("top"))
async def cmd_top(msg: Message):
    await show_top(msg, 0)


@router.callback_query(F.data.regexp(r"^top:\d+$"))
async def cb_top(cb: CallbackQuery):
    await show_top(cb, int(cb.data.split(":")[1]))


# ------------------------------------------------------------------ поиск и карточка участника
def _card_kb(tg_id, u):
    rows = []
    if u["status"] == "active" and has_perm(tg_id, "handle_violations"):
        rows.append([("⚠️ Записать нарушение", f"mem:viol:{u['id']}")])
    if u["status"] != "active" and has_perm(tg_id, "manage_members"):
        rows.append([("♻️ Восстановить", f"mem:res:{u['id']}")])
    if u["status"] == "active" and has_perm(tg_id, "delete_members"):
        rows.append([("🗑 Удалить из состава", f"mem:del:{u['id']}")])
    rows.append([("🔍 Новый поиск", "search:start"), ("🛠 Панель", "adm:menu")])
    return ikb(rows)


async def show_card(event, uid: int) -> None:
    u = db.get_user(uid)
    if not u:
        return await render(event, "Участник не найден.")
    text = profile_text(u, staff_view=True)
    viols = db.violations(uid)
    if viols:
        text += "\n\n<b>Нарушения:</b>\n" + "\n".join(
            f"• {fmt_date(v['ts'])} ({esc(v['actor_name'])}): {esc(v['details'])}" for v in viols)
    await render(event, text, _card_kb(event.from_user.id, u))


async def do_search(event, query: str) -> None:
    found = db.search_users(query)
    if not found:
        return await send_new(event, "Никого не нашёл. Попробуй другой ник, @username или ID.")
    if len(found) == 1:
        return await show_card(event, found[0]["id"])
    rows = [[(f"{r['rank_code']} · {r['roblox_name']}" + ("" if r["status"] == "active" else " (выбыл)"),
              f"mem:{r['id']}")] for r in found]
    await send_new(event, f"🔍 Найдено: {len(found)}. Выбери участника:", ikb(rows))


@router.message(Command("search"))
async def cmd_search(msg: Message, command: CommandObject, state: FSMContext):
    if not can_view_cards(msg.from_user.id):
        return await msg.answer("⛔ Нет доступа.")
    query = (command.args or "").strip()
    if query:
        return await do_search(msg, query)
    await state.set_state(Search.query)
    await msg.answer("🔍 Введи Roblox-ник, @username или Telegram ID участника:")


@router.callback_query(F.data == "search:start")
async def cb_search_start(cb: CallbackQuery, state: FSMContext):
    if not can_view_cards(cb.from_user.id):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    await state.set_state(Search.query)
    await send_new(cb, "🔍 Введи Roblox-ник, @username или Telegram ID участника:")


@router.message(Search.query, F.text, ~F.text.startswith("/"))
async def search_query(msg: Message, state: FSMContext):
    if not can_view_cards(msg.from_user.id):
        await state.clear()
        return await msg.answer("⛔ Нет доступа.")
    await state.clear()
    await do_search(msg, msg.text)


@router.callback_query(F.data.regexp(r"^mem:\d+$"))
async def cb_member(cb: CallbackQuery):
    if not can_view_cards(cb.from_user.id):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    await show_card(cb, int(cb.data.split(":")[1]))


def _can_remove(actor_id, u) -> bool:
    if u["tg_id"] == actor_id or is_owner(u["tg_id"]):
        return False
    target_staff = db.get_staff_by_tg(u["tg_id"])
    return not target_staff or can_manage_target(actor_id, target_staff)


@router.callback_query(F.data.startswith("mem:del:"))
async def cb_member_del(cb: CallbackQuery):
    if not has_perm(cb.from_user.id, "delete_members"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    u = db.get_user(int(cb.data.split(":")[2]))
    if not u or not _can_remove(cb.from_user.id, u):
        return await cb.answer("⛔ Этого участника нельзя удалить", show_alert=True)
    await render(cb, f"Удалить <b>{esc(u['roblox_name'])}</b> из состава клана?\n"
                     f"<i>История и ранги сохранятся.</i>",
                 ikb([[("✅ Да, удалить", f"mem:delok:{u['id']}"), ("✖️ Нет", f"mem:{u['id']}")]]))


@router.callback_query(F.data.startswith("mem:delok:"))
async def cb_member_del_ok(cb: CallbackQuery):
    if not has_perm(cb.from_user.id, "delete_members"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    u = db.get_user(int(cb.data.split(":")[2]))
    if not u or not _can_remove(cb.from_user.id, u):
        return await cb.answer("⛔ Этого участника нельзя удалить", show_alert=True)
    if db.set_member_status(u["id"], "removed", cb.from_user.id, actor_name(cb.from_user),
                            "Удалён администрацией"):
        lists.request_refresh()
        try:
            await cb.bot.send_message(u["tg_id"], "⛔ Ты исключён из состава клана DS администрацией.")
        except TelegramAPIError:
            pass
    await show_card(cb, u["id"])


@router.callback_query(F.data.startswith("mem:res:"))
async def cb_member_restore(cb: CallbackQuery):
    if not has_perm(cb.from_user.id, "manage_members"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    u = db.get_user(int(cb.data.split(":")[2]))
    if not u:
        return await cb.answer("Не найден", show_alert=True)
    if db.set_member_status(u["id"], "active", cb.from_user.id, actor_name(cb.from_user),
                            "Восстановлен администрацией"):
        lists.request_refresh()
        try:
            await cb.bot.send_message(u["tg_id"], "🖤 Твоя запись в составе DS восстановлена.")
        except TelegramAPIError:
            pass
    await show_card(cb, u["id"])


@router.callback_query(F.data.startswith("mem:viol:"))
async def cb_member_viol(cb: CallbackQuery, state: FSMContext):
    if not has_perm(cb.from_user.id, "handle_violations"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    uid = int(cb.data.split(":")[2])
    await state.set_state(Violation.text)
    await state.update_data(uid=uid)
    await send_new(cb, "⚠️ Опиши нарушение одним сообщением (до 500 символов). Отмена — /cancel.")


@router.message(Violation.text, F.text, ~F.text.startswith("/"))
async def violation_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not has_perm(msg.from_user.id, "handle_violations"):
        return await msg.answer("⛔ Нет доступа.")
    u = db.get_user(data.get("uid", 0))
    if not u:
        return await msg.answer("Участник не найден.")
    db.add_violation(u["id"], msg.text.strip()[:500], msg.from_user.id, actor_name(msg.from_user))
    await msg.answer("✅ Нарушение записано.", reply_markup=ikb([[("👤 К карточке", f"mem:{u['id']}")]]))


# ------------------------------------------------------------------ вход/выход из группы
async def handle_member_left(bot: Bot, u, reason: str) -> None:
    if db.set_member_status(u["id"], "left", None, SYSTEM, reason):
        lists.request_refresh()
        await notify(bot, "manage_members",
                     f"🚪 Участник <b>{esc(u['roblox_name'])}</b> ({tg_label(u['username'], u['full_name'])}) "
                     f"выбыл из состава: {esc(reason)}.")


@group_router.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_leave(event: ChatMemberUpdated, bot: Bot):
    if event.chat.id != db.get_group_id() or event.new_chat_member.user.is_bot:
        return
    u = db.get_user_by_tg(event.new_chat_member.user.id)
    if u and u["status"] == "active":
        await handle_member_left(bot, u, "вышел или был удалён из Telegram-группы")


@group_router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, bot: Bot):
    if event.chat.id != db.get_group_id() or event.new_chat_member.user.is_bot:
        return
    tg = event.new_chat_member.user
    u = db.get_user_by_tg(tg.id)
    if u is None:
        try:
            await bot.send_message(tg.id, "🖤 Добро пожаловать в группу DS! Чтобы попасть в состав, "
                                          "отправь мне свой Roblox-ник (командой /start).")
        except TelegramAPIError:
            pass  # человек ещё не писал боту — это нормально
        return
    if u["status"] == "removed":
        await notify(bot, "manage_members",
                     f"⚠️ Исключённый ранее <b>{esc(u['roblox_name'])}</b> ({tg_label(tg.username, tg.full_name)}) "
                     f"снова вступил в группу. Запись не восстановлена автоматически.")
        return
    if u["status"] != "left":
        return
    # повторное вступление: восстанавливаем запись только после проверки Roblox-аккаунта
    try:
        info = await roblox.get_user_by_id(u["roblox_id"])
    except roblox.RobloxError:
        try:
            await bot.send_message(tg.id, "⚠️ Roblox сейчас недоступен. Нажми /start чуть позже, чтобы вернуться в состав.")
        except TelegramAPIError:
            pass
        return
    if info is None or not roblox.ends_with_ds(info.name):
        await notify(bot, "manage_members",
                     f"⚠️ <b>{esc(u['roblox_name'])}</b> вернулся в группу, но проверка Roblox не пройдена "
                     f"(аккаунт недоступен или ник не заканчивается на DS). Запись не восстановлена.")
        try:
            await bot.send_message(tg.id, "❌ Не удалось восстановить запись: ник Roblox должен заканчиваться на DS. "
                                          "Исправь ник и отправь /start.")
        except TelegramAPIError:
            pass
        return
    db.update_roblox_name(u["id"], info.name, "при возвращении в группу")
    db.set_member_status(u["id"], "active", None, SYSTEM, "Вернулся в группу, проверка Roblox пройдена")
    lists.request_refresh()
    try:
        await bot.send_message(tg.id, "🖤 С возвращением в DS! Твоя запись, ранг и история восстановлены.")
    except TelegramAPIError:
        pass
    await notify(bot, "manage_members", f"♻️ Участник <b>{esc(info.name)}</b> вернулся в группу, запись восстановлена.")


@group_router.message(Command("setgroup"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_setgroup(msg: Message, bot: Bot):
    """Владелец пишет /setgroup в группе клана — бот запоминает её и публикует списки."""
    if not msg.from_user or not is_owner(msg.from_user.id):
        return
    db.set_setting("group_id", msg.chat.id)
    db.log(msg.from_user.id, actor_name(msg.from_user), "settings", f"Группа клана: {msg.chat.id}")
    warning = ""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(msg.chat.id, me.id)
        if member.status != "administrator":
            warning = "\n⚠️ Сделай бота администратором группы — иначе он не сможет публиковать списки и видеть выходы."
    except TelegramAPIError:
        pass
    lists.request_refresh()
    await msg.answer("✅ Группа привязана. Списки состава будут опубликованы и обновляются автоматически." + warning)


# ------------------------------------------------------------------ периодическая сверка
_warned_roblox = set()


async def sync_membership(bot: Bot) -> None:
    group_id = db.get_group_id()
    if not group_id:
        return
    for u in db.active_members():
        try:
            member = await bot.get_chat_member(group_id, u["tg_id"])
            if member.status in ("left", "kicked"):
                await handle_member_left(bot, u, "не найден в группе при сверке")
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramAPIError as exc:
            log.info("Сверка членства %s пропущена: %s", u["tg_id"], exc)
        await asyncio.sleep(0.1)


async def sync_roblox(bot: Bot) -> None:
    for u in db.active_members():
        try:
            info = await roblox.get_user_by_id(u["roblox_id"])
        except roblox.RobloxError:
            return  # Roblox недоступен — ничего не меняем, попробуем в следующий раз
        if info is None:
            if u["id"] not in _warned_roblox:
                _warned_roblox.add(u["id"])
                await notify(bot, "manage_members",
                             f"⚠️ Roblox-аккаунт <b>{esc(u['roblox_name'])}</b> не найден (возможно, удалён).")
        else:
            if db.update_roblox_name(u["id"], info.name, "обнаружено при сверке с Roblox"):
                lists.request_refresh()
            if not roblox.ends_with_ds(info.name) and u["id"] not in _warned_roblox:
                _warned_roblox.add(u["id"])
                await notify(bot, "manage_members",
                             f"⚠️ Ник <b>{esc(info.name)}</b> больше не заканчивается на DS.")
        await asyncio.sleep(0.5)


async def periodic_sync(bot: Bot) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await sync_membership(bot)
            await sync_roblox(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка периодической сверки состава")
        await asyncio.sleep(config.SYNC_INTERVAL_SECONDS)
