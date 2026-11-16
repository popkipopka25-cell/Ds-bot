"""Участники: регистрация, профиль, топ, поиск, учёт входа/выхода из Telegram-группы."""
import asyncio
import logging
import math
import sqlite3
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import (JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command,
                             CommandObject, CommandStart, StateFilter)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, ChatMemberUpdated, Message

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
    nick = State()      # ждём Roblox username
    confirm = State()   # показали карточку, ждём «Да / Нет»
    verify = State()    # ждём код в описании профиля Roblox


class Search(StatesGroup):
    query = State()


class Violation(StatesGroup):
    text = State()


WELCOME = ("🖤 Добро пожаловать в DS!\n\n"
           "Твой Roblox-аккаунт подтверждён.\n"
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
    await msg.answer(f"🖤 <b>DS</b>\n{SEP}\n{intro}отправь свой <b>Roblox username</b> — "
                     f"ник, которым ты входишь в аккаунт (не Display Name).\n"
                     f"Пароль от Roblox мне <b>не нужен</b> и присылать его нельзя.",
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
CODE_TTL = 30 * 60        # код живёт 30 минут
CHECK_PAUSE = 5           # минимум секунд между проверками кода (защита от лимитов Roblox)
_last_check: dict = {}


async def _say(cb: CallbackQuery, text: str, markup=None) -> None:
    """Новое сообщение в ответ на кнопку (cb.answer вызывается отдельно, ровно один раз)."""
    await cb.message.answer(text, reply_markup=markup)


def _reg_kb_confirm():
    return ikb([[("✅ Да, это мой аккаунт", "reg:yes")],
                [("❌ Нет, это не мой аккаунт", "reg:no")]])


def _reg_kb_verify():
    return ikb([[("🔍 Проверить", "reg:check")],
                [("🔄 Новый код", "reg:newcode"), ("✖️ Отмена", "reg:cancel")]])


def _card_text(info) -> str:
    return (f"🖤 <b>НАЙДЕН ROBLOX-АККАУНТ</b>\n{SEP}\n"
            f"Username: <b>{esc(info.name)}</b>\n"
            f"Display Name: <b>{esc(info.display_name)}</b>\n"
            f"Roblox User ID: <code>{info.id}</code>\n"
            f'Профиль: <a href="{info.profile_url}">{info.profile_url}</a>\n\n'
            f"Это ваш Roblox-аккаунт?")


def _verify_text(code: str) -> str:
    return (f"🔐 <b>ПОДТВЕРЖДЕНИЕ АККАУНТА</b>\n{SEP}\n"
            f"Нужно убедиться, что аккаунт принадлежит тебе.\n\n"
            f"1. Открой свой профиль Roblox и нажми «Редактировать» в блоке «Обо мне» (About).\n"
            f"2. Временно впиши в описание этот код:\n\n<code>{code}</code>\n\n"
            f"3. Сохрани описание и нажми «🔍 Проверить».\n\n"
            f"Код действует 30 минут. Пароль от Roblox вводить не нужно. "
            f"Если Roblox заменил код на «####», нажми «🔄 Новый код».")


async def _send_card(msg: Message, info, avatar_url) -> None:
    """Карточка аккаунта: официальная аватарка Roblox + данные API (без фальшивых скриншотов)."""
    text, kb = _card_text(info), _reg_kb_confirm()
    if avatar_url:
        try:
            return await msg.answer_photo(avatar_url, caption=text, reply_markup=kb)
        except TelegramAPIError as exc:
            log.info("Telegram не принял ссылку на аватарку: %s", exc)
        data = await roblox.download_image(avatar_url)
        if data:
            try:
                return await msg.answer_photo(BufferedInputFile(data, "avatar.png"),
                                              caption=text, reply_markup=kb)
            except TelegramAPIError as exc:
                log.info("Не удалось отправить аватарку файлом: %s", exc)
    await msg.answer(text, reply_markup=kb)


@router.message(StateFilter(Reg.nick, Reg.confirm, Reg.verify), F.text, ~F.text.startswith("/"))
async def reg_nick(msg: Message, state: FSMContext, bot: Bot):
    nick = msg.text.strip().lstrip("@")
    tg = msg.from_user
    if not roblox.valid_nick_format(nick):
        return await msg.answer("❌ Нужен именно <b>username</b> (имя для входа, как в настройках профиля), "
                                "а не Display Name: 3–20 символов, латиница, цифры и «_», без пробелов. "
                                "Отправь ник ещё раз.")
    await msg.answer("🔎 Проверяю аккаунт в Roblox…")
    try:
        info = await roblox.get_user_by_name(nick)
    except roblox.RobloxError:
        return await msg.answer("⚠️ Roblox сейчас недоступен или ограничил запросы. "
                                "Подожди пару минут и просто отправь ник ещё раз.")
    if info is None:
        return await msg.answer("❌ Аккаунт с таким username не найден в Roblox. "
                                "Проверь написание и отправь ник ещё раз.")

    group_id = db.get_group_id()
    if group_id:
        try:
            member = await bot.get_chat_member(group_id, tg.id)
            if member.status in ("left", "kicked"):
                return await msg.answer("Сначала вступи в Telegram-группу клана, затем отправь ник ещё раз.")
        except TelegramAPIError as exc:
            log.warning("Не удалось проверить членство в группе: %s", exc)

    problem = _registration_problem(tg.id, info.id)
    if problem:
        if problem[1]:
            await state.clear()
        return await msg.answer(problem[0], reply_markup=main_menu_kb(is_staff(tg.id)) if problem[1] else None)

    avatar_url = await roblox.get_avatar_url(info.id)
    await state.set_state(Reg.confirm)
    await state.update_data(rid=info.id, rname=info.name, rdisplay=info.display_name)
    await _send_card(msg, info, avatar_url)


def _registration_problem(tg_id: int, roblox_id: int):
    """Возвращает (текст, закончить_диалог) или None, если регистрироваться можно."""
    by_roblox = db.get_user_by_roblox(roblox_id)
    by_tg = db.get_user_by_tg(tg_id)
    if by_roblox and by_roblox["tg_id"] != tg_id:
        return ("❌ Этот Roblox-аккаунт уже привязан к другому Telegram-аккаунту. "
                "Повторная регистрация запрещена. Если это твой аккаунт — обратись к администрации.", False)
    if by_tg and by_tg["roblox_id"] != roblox_id:
        return (f"❌ Ты уже зарегистрирован с аккаунтом <b>{esc(by_tg['roblox_name'])}</b>. "
                f"Для смены аккаунта обратись к администрации.", False)
    if by_tg and by_tg["status"] == "active":
        return ("ℹ️ Ты уже в составе клана.", True)
    if by_tg and by_tg["status"] == "removed":
        return ("⛔ Твоя запись удалена администрацией. Обратись к администратору.", True)
    return None


async def _reg_data(cb: CallbackQuery, state: FSMContext, need: Reg):
    """Данные диалога регистрации или None, если сессия устарела (например, после перезапуска бота)."""
    if await state.get_state() != need.state:
        await cb.answer()
        await _say(cb, "Сессия регистрации устарела. Нажми /start и отправь ник заново.")
        return None
    data = await state.get_data()
    if not data.get("rid"):
        await cb.answer()
        await _say(cb, "Сессия регистрации устарела. Нажми /start и отправь ник заново.")
        return None
    return data


@router.callback_query(F.data == "reg:no")
async def reg_no(cb: CallbackQuery, state: FSMContext):
    if await _reg_data(cb, state, Reg.confirm) is None:
        return
    await state.set_state(Reg.nick)
    await state.update_data(rid=None)
    await cb.answer()
    await _say(cb, "Хорошо. Отправь свой Roblox <b>username</b> ещё раз.")


@router.callback_query(F.data == "reg:yes")
async def reg_yes(cb: CallbackQuery, state: FSMContext):
    data = await _reg_data(cb, state, Reg.confirm)
    if data is None:
        return
    await cb.answer()
    await _issue_code(cb, state)


async def _issue_code(cb: CallbackQuery, state: FSMContext) -> None:
    code = roblox.generate_code()
    await state.set_state(Reg.verify)
    await state.update_data(code=code, code_at=int(time.time()))
    await _say(cb, _verify_text(code), _reg_kb_verify())


@router.callback_query(F.data == "reg:newcode")
async def reg_newcode(cb: CallbackQuery, state: FSMContext):
    if await _reg_data(cb, state, Reg.verify) is None:
        return
    await cb.answer()
    await _issue_code(cb, state)


@router.callback_query(F.data == "reg:cancel")
async def reg_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Регистрация отменена")
    await _say(cb, "Регистрация отменена. Чтобы начать заново, нажми /start.")


@router.callback_query(F.data == "reg:check")
async def reg_check(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await _reg_data(cb, state, Reg.verify)
    if data is None:
        return
    tg = cb.from_user
    if time.time() - _last_check.get(tg.id, 0) < CHECK_PAUSE:
        return await cb.answer("Подожди пару секунд и нажми ещё раз.")
    _last_check[tg.id] = time.time()

    if int(time.time()) - int(data.get("code_at", 0)) > CODE_TTL:
        await cb.answer("Код устарел")
        await _say(cb, "⌛ Срок действия кода истёк. Выдаю новый.")
        return await _issue_code(cb, state)

    await cb.answer("Проверяю…")
    try:
        info = await roblox.get_user_by_id(int(data["rid"]))
    except roblox.RobloxError:
        return await _say(cb, "⚠️ Roblox сейчас недоступен или ограничил запросы. "
                              "Подожди минуту и нажми «🔍 Проверить» ещё раз.", _reg_kb_verify())
    if info is None:
        await state.clear()
        return await _say(cb, "❌ Аккаунт больше не найден в Roblox. Нажми /start и начни заново.")
    if info.is_banned:
        await state.clear()
        return await _say(cb, "❌ Этот Roblox-аккаунт заблокирован, подтвердить его невозможно.")
    if not roblox.code_in_description(data["code"], info.description):
        return await _say(
            cb, "❌ Код не найден в описании профиля.\n\n"
                f"Убедись, что в блоке «Обо мне» есть <code>{data['code']}</code>, описание сохранено "
                f"и аккаунт — <b>{esc(info.name)}</b>. Иногда Roblox обновляет описание с задержкой "
                f"в несколько секунд.", _reg_kb_verify())

    # ---- владение подтверждено: только теперь регистрируем
    problem = _registration_problem(tg.id, info.id)
    if problem:
        await state.clear()
        return await _say(cb, problem[0])
    by_tg = db.get_user_by_tg(tg.id)
    try:
        if by_tg:  # возврат выбывшего участника: ранг и история PvP сохраняются
            db.update_roblox_name(by_tg["id"], info.name, "при повторной регистрации")
            db.update_roblox_display_name(by_tg["id"], info.display_name)
            db.set_member_status(by_tg["id"], "active", tg.id, actor_name(tg), "Повторная регистрация")
            event = f"♻️ Восстановлен участник <b>{esc(info.name)}</b> ({tg_label(tg.username, tg.full_name)})."
        else:
            db.create_user(tg.id, tg.username, tg.full_name, info.id, info.name, actor_name(tg),
                           display_name=info.display_name)
            event = f"➕ Новый участник: <b>{esc(info.name)}</b> ({tg_label(tg.username, tg.full_name)})."
    except sqlite3.IntegrityError:
        await state.clear()
        return await _say(cb, "❌ Этот Roblox-аккаунт уже привязан к другому Telegram-аккаунту.")

    await state.clear()
    lists.request_refresh()
    await _say(cb, "✅ <b>Аккаунт подтверждён!</b>\n\n"
                   "Теперь, пожалуйста, <b>удали временный код</b> из описания профиля Roblox — "
                   "он больше не нужен.")
    await _say(cb, WELCOME, main_menu_kb(is_staff(tg.id)))
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
        *([f"Display Name: {esc(u['roblox_display_name'])}"] if u["roblox_display_name"] else []),
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
    if info is None:
        await notify(bot, "manage_members",
                     f"⚠️ <b>{esc(u['roblox_name'])}</b> вернулся в группу, но Roblox-аккаунт не найден "
                     f"(возможно, удалён). Запись не восстановлена.")
        try:
            await bot.send_message(tg.id, "❌ Не удалось восстановить запись: твой Roblox-аккаунт не найден. "
                                          "Обратись к администрации.")
        except TelegramAPIError:
            pass
        return
    db.update_roblox_name(u["id"], info.name, "при возвращении в группу")
    db.update_roblox_display_name(u["id"], info.display_name)
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
            if db.update_roblox_display_name(u["id"], info.display_name):
                lists.request_refresh()
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
