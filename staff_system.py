"""Работники клана, должности и индивидуальные разрешения.

Все права проверяются на стороне сервера (в каждом обработчике), а не по виду кнопок.
Владелец (OWNER_ID) имеет все права и единственный может менять разрешения.
"""
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
                           CallbackQuery, Message)

import config
from database import db
from message_manager import lists
from utils import SEP, actor_name, esc, fmt_dt, ikb, render

log = logging.getLogger(__name__)
router = Router(name="staff")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


# ------------------------------------------------------------------ проверка прав
def is_owner(tg_id) -> bool:
    return config.OWNER_ID is not None and tg_id == config.OWNER_ID


def has_perm(tg_id, perm) -> bool:
    return is_owner(tg_id) or db.has_permission(tg_id, perm)


def has_any(tg_id, *perms) -> bool:
    return is_owner(tg_id) or any(db.has_permission(tg_id, p) for p in perms)


def is_staff(tg_id) -> bool:
    return is_owner(tg_id) or db.get_staff_by_tg(tg_id) is not None


def position_label(tg_id):
    if is_owner(tg_id):
        return "Владелец"
    row = db.get_staff_by_tg(tg_id)
    return config.POSITIONS.get(row["position"]) if row else None


def recipients_with(*perms) -> list:
    ids = set(db.staff_tg_with_perms(perms))
    if config.OWNER_ID:
        ids.add(config.OWNER_ID)
    return list(ids)


async def notify(bot: Bot, perms, text: str, markup=None, skip=None) -> None:
    perms = (perms,) if isinstance(perms, str) else tuple(perms)
    for tg_id in recipients_with(*perms):
        if tg_id == skip:
            continue
        try:
            await bot.send_message(tg_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.info("Не удалось уведомить %s: %s", tg_id, exc)


def can_manage_target(actor_id, staff_row) -> bool:
    if is_owner(actor_id):
        return True
    if staff_row["tg_id"] == actor_id:
        return False  # нельзя менять самого себя
    return staff_row["position"] != "chief_admin"


def assignable_positions(actor_id) -> list:
    if is_owner(actor_id):
        return list(config.POSITION_ORDER)
    return [p for p in config.POSITION_ORDER if p != "chief_admin"]


# ------------------------------------------------------------------ меню команд Telegram
BASE_COMMANDS = [
    BotCommand(command="start", description="Запуск / регистрация"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="rank", description="Мой PvP-ранг"),
    BotCommand(command="ranklist", description="Таблица рангов"),
    BotCommand(command="top", description="Топ игроков"),
    BotCommand(command="help", description="Помощь"),
]


def commands_for(tg_id) -> list:
    cmds = list(BASE_COMMANDS)
    if not is_staff(tg_id):
        return cmds
    cmds.append(BotCommand(command="admin", description="Панель управления"))
    if has_perm(tg_id, "view_stats"):
        cmds.append(BotCommand(command="stats", description="Статистика"))
    if has_perm(tg_id, "view_history"):
        cmds.append(BotCommand(command="history", description="История действий"))
    if has_perm(tg_id, "view_members"):
        cmds.append(BotCommand(command="ranks", description="Общий состав"))
    if has_perm(tg_id, "manage_staff"):
        cmds.append(BotCommand(command="staff", description="Работники"))
    if has_any(tg_id, "view_profiles", "help_members", "manage_members"):
        cmds.append(BotCommand(command="search", description="Поиск участника"))
    if has_any(tg_id, "pvp_view", "pvp_accept", "pvp_result", "manage_requests"):
        cmds.append(BotCommand(command="pvp", description="Заявки на PvP"))
    return cmds


async def apply_commands(bot: Bot, tg_id) -> None:
    if not tg_id:
        return
    try:
        await bot.set_my_commands(commands_for(tg_id), scope=BotCommandScopeChat(chat_id=tg_id))
    except TelegramAPIError as exc:
        log.info("Меню команд для %s не обновлено: %s", tg_id, exc)


async def setup_default_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BASE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except TelegramAPIError as exc:
        log.warning("Не удалось задать меню команд: %s", exc)
    await apply_commands(bot, config.OWNER_ID)
    for s in db.list_staff():
        await apply_commands(bot, s["tg_id"])


# ------------------------------------------------------------------ панель /staff
class AddStaff(StatesGroup):
    target = State()
    position = State()


def _staff_name(s) -> str:
    if s["roblox_name"]:
        return s["roblox_name"]
    if s["username"]:
        return "@" + s["username"]
    return f"ID {s['tg_id']}"


def _staff_tg(s) -> str:
    if s["username"]:
        return "@" + esc(s["username"])
    return f"ID {s['tg_id']}" if s["tg_id"] else "—"


def _menu_kb():
    return ikb([
        [("➕ Добавить работника", "st:add")],
        [("➖ Удалить работника", "st:del")],
        [("👥 Список работников", "st:list")],
        [("✏️ Изменить должность", "st:edit")],
        [("🔍 Информация о работнике", "st:info")],
        [("⬅️ Панель управления", "adm:menu")],
    ])


async def show_menu(event) -> None:
    await render(event, f"🖤 <b>РАБОТНИКИ DS</b>\n{SEP}\nВыбери действие:", _menu_kb())


async def guard(cb: CallbackQuery) -> bool:
    if not has_perm(cb.from_user.id, "manage_staff"):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return False
    return True


def _pick_kb(prefix: str, staff_rows) -> object:
    rows = [[(f"{config.POSITIONS[s['position']]} · {_staff_name(s)}"[:60], f"{prefix}:{s['id']}")]
            for s in staff_rows[:50]]
    rows.append([("⬅️ Назад", "st:menu")])
    return ikb(rows)


@router.message(Command("staff"))
async def cmd_staff(msg: Message):
    if not has_perm(msg.from_user.id, "manage_staff"):
        return await msg.answer("⛔ Нет доступа.")
    await show_menu(msg)


@router.callback_query(F.data == "st:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    if not await guard(cb):
        return
    await state.clear()
    await show_menu(cb)


@router.callback_query(F.data == "st:list")
async def cb_list(cb: CallbackQuery):
    if not await guard(cb):
        return
    staff = db.list_staff()
    lines = [f"👑 <b>Владелец</b> — @{esc(config.OWNER_USERNAME)}", ""]
    for pos in config.POSITION_ORDER:
        for s in [x for x in staff if x["position"] == pos]:
            lines.append(f"• <b>{config.POSITIONS[pos]}</b> — {esc(_staff_name(s))}")
    if not staff:
        lines.append("Работников пока нет.")
    await render(cb, f"🖤 <b>СПИСОК РАБОТНИКОВ</b>\n{SEP}\n" + "\n".join(lines),
                 ikb([[("⬅️ Назад", "st:menu")]]))


# ---- добавление
@router.callback_query(F.data == "st:add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if not await guard(cb):
        return
    await state.set_state(AddStaff.target)
    await render(cb, "➕ <b>Добавление работника</b>\n\nОтправь Telegram ID (цифры) или @username "
                     "человека.\nЕсли указать @username, права заработают, как только он напишет боту.",
                 ikb([[("✖️ Отмена", "st:menu")]]))


@router.message(AddStaff.target, F.text, ~F.text.startswith("/"))
async def add_target(msg: Message, state: FSMContext):
    if not has_perm(msg.from_user.id, "manage_staff"):
        await state.clear()
        return await msg.answer("⛔ Нет доступа.")
    text = msg.text.strip()
    tg_id, username = None, None
    if re.fullmatch(r"\d{5,15}", text):
        tg_id = int(text)
    else:
        m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{4,31})", text)
        if not m:
            return await msg.answer("❌ Не похоже на ID или @username. Попробуй ещё раз.")
        username = m.group(1)

    if tg_id and is_owner(tg_id) or (username and username.lower() == config.OWNER_USERNAME.lower()):
        return await msg.answer("👑 Владелец уже имеет все права.")
    if username and not tg_id:
        known = db.get_user_by_username(username)
        if known:
            tg_id = known["tg_id"]
    if tg_id and not username:
        known = db.get_user_by_tg(tg_id)
        if known and known["username"]:
            username = known["username"]
    if (tg_id and db.get_staff_by_tg(tg_id)) or (username and db.find_staff_by_username(username)):
        return await msg.answer("ℹ️ Этот человек уже работник. Измени должность через меню.")
    if tg_id and is_owner(tg_id):
        return await msg.answer("👑 Владелец уже имеет все права.")

    await state.set_state(AddStaff.position)
    await state.update_data(tg_id=tg_id, username=username)
    rows = [[(config.POSITIONS[p], f"st:pos:{p}")] for p in assignable_positions(msg.from_user.id)]
    rows.append([("✖️ Отмена", "st:menu")])
    who = f"@{esc(username)}" if username else f"ID {tg_id}"
    await msg.answer(f"Выбери должность для <b>{who}</b>:", reply_markup=ikb(rows))


@router.callback_query(AddStaff.position, F.data.startswith("st:pos:"))
async def add_position(cb: CallbackQuery, state: FSMContext):
    if not await guard(cb):
        return
    code = cb.data.split(":")[2]
    if code not in assignable_positions(cb.from_user.id):
        return await cb.answer("⛔ Эту должность может выдать только владелец", show_alert=True)
    data = await state.get_data()
    await state.clear()
    tg_id, username = data.get("tg_id"), data.get("username")
    if (tg_id and db.get_staff_by_tg(tg_id)) or (username and db.find_staff_by_username(username)):
        return await render(cb, "ℹ️ Этот человек уже работник.", ikb([[("⬅️ Назад", "st:menu")]]))
    who = f"@{username}" if username else f"ID {tg_id}"
    try:
        db.add_staff(tg_id, username, code, config.DEFAULT_PERMS[code], cb.from_user.id,
                     actor_name(cb.from_user), f"{who}: {config.POSITIONS[code]}")
    except Exception:
        log.exception("Не удалось добавить работника")
        return await render(cb, "❌ Не удалось добавить работника.", ikb([[("⬅️ Назад", "st:menu")]]))
    lists.request_refresh()
    await apply_commands(cb.bot, tg_id)
    if tg_id:
        try:
            await cb.bot.send_message(
                tg_id, f"🖤 Тебе назначена должность: <b>{config.POSITIONS[code]}</b>.\n"
                       f"Открой панель командой /admin.")
        except TelegramAPIError:
            pass
    await render(cb, f"✅ Добавлен: <b>{esc(who)}</b> — {config.POSITIONS[code]}.",
                 ikb([[("⬅️ Назад", "st:menu")]]))


# ---- удаление
@router.callback_query(F.data == "st:del")
async def cb_del(cb: CallbackQuery):
    if not await guard(cb):
        return
    rows = [s for s in db.list_staff() if can_manage_target(cb.from_user.id, s)]
    if not rows:
        return await render(cb, "Некого удалять.", ikb([[("⬅️ Назад", "st:menu")]]))
    await render(cb, "➖ Кого удалить?", _pick_kb("st:delask", rows))


@router.callback_query(F.data.startswith("st:delask:"))
async def cb_del_ask(cb: CallbackQuery):
    if not await guard(cb):
        return
    s = db.get_staff(int(cb.data.split(":")[2]))
    if not s or not can_manage_target(cb.from_user.id, s):
        return await cb.answer("⛔ Недоступно", show_alert=True)
    await render(cb, f"Удалить работника <b>{esc(_staff_name(s))}</b> "
                     f"({config.POSITIONS[s['position']]})?",
                 ikb([[("✅ Да, удалить", f"st:delok:{s['id']}"), ("✖️ Нет", "st:menu")]]))


@router.callback_query(F.data.startswith("st:delok:"))
async def cb_del_ok(cb: CallbackQuery):
    if not await guard(cb):
        return
    s = db.get_staff(int(cb.data.split(":")[2]))
    if not s or not can_manage_target(cb.from_user.id, s):
        return await cb.answer("⛔ Недоступно", show_alert=True)
    db.remove_staff(s["id"], cb.from_user.id, actor_name(cb.from_user),
                    f"{_staff_name(s)}: {config.POSITIONS[s['position']]}")
    lists.request_refresh()
    if s["tg_id"]:
        try:
            await cb.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=s["tg_id"]))
            await cb.bot.send_message(s["tg_id"], "ℹ️ Ты больше не работник клана DS.")
        except TelegramAPIError:
            pass
    await render(cb, f"✅ Работник <b>{esc(_staff_name(s))}</b> удалён.", ikb([[("⬅️ Назад", "st:menu")]]))


# ---- смена должности
@router.callback_query(F.data == "st:edit")
async def cb_edit(cb: CallbackQuery):
    if not await guard(cb):
        return
    rows = [s for s in db.list_staff() if can_manage_target(cb.from_user.id, s)]
    if not rows:
        return await render(cb, "Некого редактировать.", ikb([[("⬅️ Назад", "st:menu")]]))
    await render(cb, "✏️ Чью должность изменить?", _pick_kb("st:editsel", rows))


@router.callback_query(F.data.startswith("st:editsel:"))
async def cb_edit_sel(cb: CallbackQuery):
    if not await guard(cb):
        return
    s = db.get_staff(int(cb.data.split(":")[2]))
    if not s or not can_manage_target(cb.from_user.id, s):
        return await cb.answer("⛔ Недоступно", show_alert=True)
    rows = [[(config.POSITIONS[p], f"st:editset:{s['id']}:{p}")] for p in assignable_positions(cb.from_user.id)]
    rows.append([("⬅️ Назад", "st:menu")])
    await render(cb, f"Новая должность для <b>{esc(_staff_name(s))}</b>\n"
                     f"Сейчас: {config.POSITIONS[s['position']]}\n"
                     f"<i>Разрешения будут сброшены до стандартных для новой должности.</i>", ikb(rows))


@router.callback_query(F.data.startswith("st:editset:"))
async def cb_edit_set(cb: CallbackQuery):
    if not await guard(cb):
        return
    _, _, sid, code = cb.data.split(":")
    s = db.get_staff(int(sid))
    if not s or not can_manage_target(cb.from_user.id, s) or code not in assignable_positions(cb.from_user.id):
        return await cb.answer("⛔ Недоступно", show_alert=True)
    db.set_staff_position(s["id"], code, config.DEFAULT_PERMS[code], cb.from_user.id,
                          actor_name(cb.from_user),
                          f"{_staff_name(s)}: {config.POSITIONS[s['position']]} → {config.POSITIONS[code]}")
    lists.request_refresh()
    await apply_commands(cb.bot, s["tg_id"])
    if s["tg_id"]:
        try:
            await cb.bot.send_message(s["tg_id"], f"🖤 Твоя должность изменена: <b>{config.POSITIONS[code]}</b>.")
        except TelegramAPIError:
            pass
    await render(cb, f"✅ <b>{esc(_staff_name(s))}</b> — теперь {config.POSITIONS[code]}.",
                 ikb([[("⬅️ Назад", "st:menu")]]))


# ---- информация и разрешения
@router.callback_query(F.data == "st:info")
async def cb_info(cb: CallbackQuery):
    if not await guard(cb):
        return
    rows = db.list_staff()
    if not rows:
        return await render(cb, "Работников пока нет.", ikb([[("⬅️ Назад", "st:menu")]]))
    await render(cb, "🔍 О ком показать информацию?", _pick_kb("st:infosel", rows))


async def _show_info(cb: CallbackQuery, staff_id: int):
    s = db.get_staff(staff_id)
    if not s:
        return await cb.answer("Работник не найден", show_alert=True)
    perms = db.get_perms(staff_id)
    lines = [f"{'✅' if p in perms else '▫️'} {config.PERMISSIONS[p]}" for p in config.PERMISSION_ORDER]
    text = (f"🖤 <b>РАБОТНИК</b>\n{SEP}\n"
            f"Имя: <b>{esc(_staff_name(s))}</b>\n"
            f"Telegram: {_staff_tg(s)}\n"
            f"Должность: {config.POSITIONS[s['position']]}\n"
            f"Добавлен: {fmt_dt(s['added_at'])}\n\n<b>Разрешения:</b>\n" + "\n".join(lines))
    rows = []
    if is_owner(cb.from_user.id):
        rows.append([("⚙️ Изменить разрешения", f"st:perms:{staff_id}")])
    rows.append([("⬅️ Назад", "st:menu")])
    await render(cb, text, ikb(rows))


@router.callback_query(F.data.startswith("st:infosel:"))
async def cb_info_sel(cb: CallbackQuery):
    if not await guard(cb):
        return
    await _show_info(cb, int(cb.data.split(":")[2]))


async def _show_perms(cb: CallbackQuery, staff_id: int):
    s = db.get_staff(staff_id)
    if not s:
        return await cb.answer("Работник не найден", show_alert=True)
    perms = db.get_perms(staff_id)
    rows = [[(f"{'✅' if p in perms else '❌'} {config.PERMISSIONS[p]}", f"st:ptog:{staff_id}:{i}")]
            for i, p in enumerate(config.PERMISSION_ORDER)]
    rows.append([("🔄 Сбросить по должности", f"st:preset:{staff_id}")])
    rows.append([("⬅️ Назад", f"st:infosel:{staff_id}")])
    await render(cb, f"⚙️ <b>Разрешения: {esc(_staff_name(s))}</b>\n"
                     f"Должность: {config.POSITIONS[s['position']]}\nНажми, чтобы включить/выключить:",
                 ikb(rows))


@router.callback_query(F.data.startswith("st:perms:"))
async def cb_perms(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("⛔ Разрешения меняет только владелец", show_alert=True)
    await _show_perms(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("st:ptog:"))
async def cb_perm_toggle(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("⛔ Разрешения меняет только владелец", show_alert=True)
    _, _, sid, idx = cb.data.split(":")
    s = db.get_staff(int(sid))
    if not s or not (0 <= int(idx) < len(config.PERMISSION_ORDER)):
        return await cb.answer("Недоступно", show_alert=True)
    perm = config.PERMISSION_ORDER[int(idx)]
    perms = db.get_perms(s["id"])
    verb = "выключено" if perm in perms else "включено"
    perms.symmetric_difference_update({perm})
    db.set_perms(s["id"], perms, cb.from_user.id, actor_name(cb.from_user),
                 f"{_staff_name(s)}: «{config.PERMISSIONS[perm]}» {verb}")
    lists.request_refresh()
    await apply_commands(cb.bot, s["tg_id"])
    await _show_perms(cb, s["id"])


@router.callback_query(F.data.startswith("st:preset:"))
async def cb_perm_preset(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("⛔ Разрешения меняет только владелец", show_alert=True)
    s = db.get_staff(int(cb.data.split(":")[2]))
    if not s:
        return await cb.answer("Работник не найден", show_alert=True)
    db.set_perms(s["id"], config.DEFAULT_PERMS[s["position"]], cb.from_user.id,
                 actor_name(cb.from_user), f"{_staff_name(s)}: разрешения сброшены по должности")
    lists.request_refresh()
    await apply_commands(cb.bot, s["tg_id"])
    await _show_perms(cb, s["id"])
