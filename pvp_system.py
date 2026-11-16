"""PvP-проверки: заявки, принятие проверяющим, внесение результатов, автоматический ранг."""
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
from database import CooldownError, db
from message_manager import lists
from rank_system import MATCH_TARGET, parse_score, rank_label, rank_table_text, validate_scores
from staff_system import has_any, has_perm, is_owner, notify
from utils import (SEP, actor_name, contact, cooldown_days_text, esc, fmt_date, fmt_dt, fmt_remaining,
                   ikb, now, render, send_new, tg_label)

log = logging.getLogger(__name__)
router = Router(name="pvp")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class Pvp(StatesGroup):
    mine = State()
    his = State()
    confirm = State()


def _cooldown_message(remaining: int) -> str:
    return ("⏳ <b>Ты пока не можешь пройти повторную проверку.</b>\n\n"
            f"До следующей проверки осталось: {fmt_remaining(remaining)}")


# ------------------------------------------------------------------ сторона участника
async def show_rank(event) -> None:
    u = db.get_user_by_tg(event.from_user.id)
    if not u or u["status"] != "active":
        return await render(event, "Ты не в составе клана. Нажми /start, чтобы зарегистрироваться.")
    ts = now()
    nxt = u["next_check_at"]
    nxt_txt = (f"{fmt_dt(nxt)} (через {fmt_remaining(nxt - ts)})" if nxt and nxt > ts
               else "доступна сейчас")
    lines = [f"🖤 <b>PvP-РАНГ</b>\n{SEP}",
             f"Текущий ранг: <b>{rank_label(u['rank_code'])}</b>",
             f"Последняя проверка: {fmt_dt(u['last_pvp_at'])}",
             f"Следующая проверка: {nxt_txt}"]
    open_req = db.user_open_request(u["id"])
    if open_req:
        state = "ожидает проверяющего" if open_req["status"] == "pending" else "принята, проверяющий свяжется с тобой"
        lines.append(f"Активная заявка: #{open_req['id']} — {state}")
    lines.append("")
    lines.append("<b>История проверок:</b>")
    matches = db.user_matches(u["id"], 5)
    if matches:
        for m in matches:
            lines.append(f"• {fmt_date(m['created_at'])}: проверяющий {m['checker_wins']} — ты {m['user_wins']} "
                         f"→ {m['new_rank']}")
    else:
        lines.append("• Проверок ещё не было")
    first = [("❌ Отменить заявку", "rank:cancel")] if open_req else [("🎮 Получить ранг", "rank:apply")]
    await render(event, "\n".join(lines), ikb([first, [("🖤 Меню", "menu:main")]]))


@router.message(Command("rank"))
async def cmd_rank(msg: Message):
    await show_rank(msg)


@router.callback_query(F.data == "rank:show")
async def cb_rank(cb: CallbackQuery):
    await show_rank(cb)


@router.message(Command("ranklist"))
async def cmd_ranklist(msg: Message):
    await msg.answer(f"🖤 <b>PvP-РАНГИ DS</b>\n{SEP}\n{rank_table_text()}\n\n"
                     f"Проверка идёт до {MATCH_TARGET} побед. Ранг считается автоматически по победам участника. "
                     f"Повторная проверка — через {cooldown_days_text()}.")


async def apply_for_rank(event, bot: Bot) -> None:
    u = db.get_user_by_tg(event.from_user.id)
    if not u or u["status"] != "active":
        return await render(event, "Ты не в составе клана. Нажми /start, чтобы зарегистрироваться.")
    try:
        rid = db.create_request(u["id"])
    except CooldownError as exc:
        return await send_new(event, _cooldown_message(exc.remaining))
    except ValueError as exc:
        return await send_new(event, f"ℹ️ {esc(exc)}")
    lists.request_refresh()
    await render(event, f"✅ Заявка #{rid} создана. Проверяющий свяжется с тобой в Telegram.",
                 ikb([[("🎮 PvP-ранг", "rank:show")]]))
    await notify(bot, ("pvp_view", "pvp_accept"),
                 f"🎮 <b>Новая заявка на PvP #{rid}</b>\n{esc(u['roblox_name'])} — {rank_label(u['rank_code'])}",
                 ikb([[("🎮 Открыть заявки", "pvp:panel")]]), skip=u["tg_id"])


@router.message(Command("rankrequest"))
async def cmd_rankrequest(msg: Message, bot: Bot):
    await apply_for_rank(msg, bot)


@router.callback_query(F.data == "rank:apply")
async def cb_apply(cb: CallbackQuery, bot: Bot):
    await apply_for_rank(cb, bot)


@router.callback_query(F.data == "rank:cancel")
async def cb_cancel_own(cb: CallbackQuery):
    u = db.get_user_by_tg(cb.from_user.id)
    open_req = db.user_open_request(u["id"]) if u else None
    if not open_req:
        return await cb.answer("Активной заявки нет", show_alert=True)
    db.cancel_request(open_req["id"], cb.from_user.id, actor_name(cb.from_user), only_user_id=u["id"])
    if open_req["accepted_by"]:
        try:
            await cb.bot.send_message(open_req["accepted_by"],
                                      f"ℹ️ Участник {esc(open_req['roblox_name'])} отменил заявку #{open_req['id']}.")
        except TelegramAPIError:
            pass
    lists.request_refresh()
    await show_rank(cb)


# ------------------------------------------------------------------ сторона проверяющего
PVP_PERMS = ("pvp_view", "pvp_accept", "pvp_result", "manage_requests")


async def show_panel(event) -> None:
    tg = event.from_user.id
    if not has_any(tg, *PVP_PERMS):
        return await render(event, "⛔ Нет доступа.")
    pending = db.pending_requests()
    all_accepted = is_owner(tg) or has_perm(tg, "manage_requests")
    accepted = db.accepted_requests(None if all_accepted else tg)
    rows = [[(f"🆕 #{r['id']} · {r['roblox_name']} ({r['rank_code']})", f"pvp:view:{r['id']}")] for r in pending[:30]]
    rows += [[(f"⏳ #{r['id']} · {r['roblox_name']} — в работе", f"pvp:view:{r['id']}")] for r in accepted[:30]]
    rows.append([("🔄 Обновить", "pvp:panel"), ("🛠 Панель", "adm:menu")])
    await render(event, f"🎮 <b>ЗАЯВКИ НА PvP</b>\n{SEP}\nНовых: <b>{len(pending)}</b>\n"
                        f"В работе: <b>{len(accepted)}</b>", ikb(rows))


@router.message(Command("pvp"))
async def cmd_pvp(msg: Message):
    await show_panel(msg)


@router.callback_query(F.data == "pvp:panel")
async def cb_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_panel(cb)


async def show_request(event, rid: int) -> None:
    tg = event.from_user.id
    if not has_any(tg, *PVP_PERMS):
        return await render(event, "⛔ Нет доступа.")
    r = db.get_request(rid)
    if not r:
        return await render(event, "Заявка не найдена.", ikb([[("⬅️ К заявкам", "pvp:panel")]]))
    status = {"pending": "ожидает проверяющего", "accepted": "принята", "done": "завершена",
              "cancelled": "отменена"}[r["status"]]
    lines = [f"🎮 <b>Заявка #{r['id']}</b>\n{SEP}",
             f"Участник: <b>{esc(r['roblox_name'])}</b> ({tg_label(r['user_username'])})",
             f"Текущий ранг: {rank_label(r['rank_code'])}",
             f"Последняя проверка: {fmt_dt(r['last_pvp_at'])}",
             f"Создана: {fmt_dt(r['created_at'])}",
             f"Статус: {status}"]
    rows = []
    if r["status"] == "pending":
        if has_perm(tg, "pvp_accept"):
            rows.append([("✅ Принять заявку", f"pvp:accept:{rid}")])
    elif r["status"] == "accepted":
        lines.append(f"Связаться с участником: {contact(r['user_tg'], r['user_username'])}")
        mine = r["accepted_by"] == tg or is_owner(tg)
        if mine and has_perm(tg, "pvp_result"):
            rows.append([("📝 Внести результат", f"pvp:enter:{rid}")])
        if mine or has_perm(tg, "manage_requests"):
            rows.append([("↩️ Вернуть в очередь", f"pvp:release:{rid}")])
    if r["status"] in ("pending", "accepted") and has_perm(tg, "manage_requests"):
        rows.append([("🗑 Отменить заявку", f"pvp:cancel:{rid}")])
    rows.append([("⬅️ К заявкам", "pvp:panel")])
    await render(event, "\n".join(lines), ikb(rows))


@router.callback_query(F.data.startswith("pvp:view:"))
async def cb_view(cb: CallbackQuery):
    await show_request(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("pvp:accept:"))
async def cb_accept(cb: CallbackQuery):
    if not has_perm(cb.from_user.id, "pvp_accept"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    rid = int(cb.data.split(":")[2])
    try:
        r = db.accept_request(rid, cb.from_user.id, actor_name(cb.from_user))
    except ValueError as exc:
        await cb.answer(str(exc), show_alert=True)
        return await show_panel(cb)
    lists.request_refresh()
    try:
        await cb.bot.send_message(
            r["user_tg"], f"🎮 Твою заявку на PvP #{rid} принял проверяющий "
                          f"{contact(cb.from_user.id, cb.from_user.username)}. Он свяжется с тобой.")
    except TelegramAPIError:
        pass
    await show_request(cb, rid)


@router.callback_query(F.data.startswith("pvp:release:"))
async def cb_release(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    force = is_owner(cb.from_user.id) or has_perm(cb.from_user.id, "manage_requests")
    if not has_any(cb.from_user.id, "pvp_accept", "manage_requests") or \
            not db.release_request(rid, cb.from_user.id, actor_name(cb.from_user), force=force):
        return await cb.answer("Не получилось вернуть заявку", show_alert=True)
    await show_request(cb, rid)


@router.callback_query(F.data.startswith("pvp:cancel:"))
async def cb_cancel(cb: CallbackQuery):
    if not has_perm(cb.from_user.id, "manage_requests"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    rid = int(cb.data.split(":")[2])
    r = db.cancel_request(rid, cb.from_user.id, actor_name(cb.from_user))
    if not r:
        return await cb.answer("Заявка уже закрыта", show_alert=True)
    lists.request_refresh()
    try:
        await cb.bot.send_message(r["user_tg"], f"ℹ️ Твоя заявка на PvP #{rid} отменена администрацией.")
    except TelegramAPIError:
        pass
    await show_panel(cb)


# ---- ввод результата
def _abort_kb():
    return ikb([[("✖️ Отмена", "pvp:panel")]])


@router.callback_query(F.data.startswith("pvp:enter:"))
async def cb_enter(cb: CallbackQuery, state: FSMContext):
    tg = cb.from_user.id
    if not has_perm(tg, "pvp_result"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    rid = int(cb.data.split(":")[2])
    r = db.get_request(rid)
    if not r or r["status"] != "accepted" or (r["accepted_by"] != tg and not is_owner(tg)):
        return await cb.answer("Заявка недоступна для внесения результата", show_alert=True)
    await state.set_state(Pvp.mine)
    await state.update_data(rid=rid)
    await render(cb, f"📝 <b>Результат PvP — {esc(r['roblox_name'])}</b>\n\n"
                     f"Матч идёт до {MATCH_TARGET} побед.\nВведи <b>«Мои победы»</b> (число от 0 до {MATCH_TARGET}):",
                 _abort_kb())


@router.message(Pvp.mine, F.text, ~F.text.startswith("/"))
async def enter_mine(msg: Message, state: FSMContext):
    try:
        value = parse_score(msg.text)
    except ValueError as exc:
        return await msg.answer(f"❌ {exc}")
    await state.update_data(mine=value)
    await state.set_state(Pvp.his)
    await msg.answer(f"Теперь введи <b>«Его победы»</b> — победы участника (0–{MATCH_TARGET}):",
                     reply_markup=_abort_kb())


@router.message(Pvp.his, F.text, ~F.text.startswith("/"))
async def enter_his(msg: Message, state: FSMContext):
    try:
        value = parse_score(msg.text)
        data = await state.get_data()
        validate_scores(data["mine"], value)
    except ValueError as exc:
        return await msg.answer(f"❌ {exc}")
    await state.update_data(his=value)
    await state.set_state(Pvp.confirm)
    await msg.answer(f"Проверь результат:\n\nМои победы: <b>{data['mine']}</b>\nЕго победы: <b>{value}</b>\n\n"
                     f"Ранг будет рассчитан автоматически. Подтвердить?",
                     reply_markup=ikb([[("✅ Подтвердить", "pvp:confirm"), ("✖️ Отмена", "pvp:panel")]]))


@router.callback_query(Pvp.confirm, F.data == "pvp:confirm")
async def cb_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    tg = cb.from_user.id
    if not has_perm(tg, "pvp_result"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    try:
        res = db.complete_pvp(data["rid"], tg, actor_name(cb.from_user), data["mine"], data["his"])
    except (ValueError, PermissionError) as exc:
        return await render(cb, f"❌ {esc(exc)}", ikb([[("⬅️ К заявкам", "pvp:panel")]]))
    lists.request_refresh()
    verdict = {"up": "⬆️ ранг повышен", "down": "⬇️ ранг понижен", "same": "ранг не изменился"}[res["change"]]
    await render(cb, f"✅ Результат записан.\nНовый ранг участника: <b>{rank_label(res['new_rank'])}</b> ({verdict}).",
                 ikb([[("⬅️ К заявкам", "pvp:panel")]]))
    try:
        await cb.bot.send_message(
            res["user_tg"],
            f"🎮 <b>PvP завершено!</b>\n\nТвой результат:\nПобеды проверяющего: {data['mine']}\n"
            f"Твои победы: {data['his']}\n\n🖤 Твой новый ранг: <b>{rank_label(res['new_rank'])}</b>.\n\n"
            f"Следующая проверка будет доступна через {cooldown_days_text()}")
    except TelegramAPIError:
        log.info("Не удалось уведомить участника %s о результате", res["user_tg"])
