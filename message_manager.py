"""Автоматическая публикация и обновление списков состава в Telegram-группе.

Принцип: каждое сообщение со списком создаётся один раз, его ID хранится в таблице messages.
После любого изменения базы бот редактирует старые сообщения (editMessageText).
Если сообщение удалили вручную — создаётся новое. Раз в RECONCILE_INTERVAL идёт сверка.
"""
import asyncio
import hashlib
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

import config
from database import db
from rank_system import RANKS, rank_label
from utils import SEP, esc, tg_label

log = logging.getLogger(__name__)

RANK_CODES = [code for code, *_ in RANKS]
LIST_KEYS = ["all", *RANK_CODES, "staff", "admins", "checkers"]
PAGE_LIMIT = 3500  # запас до лимита Telegram в 4096 символов


def topic_for(list_key: str):
    """ID темы форума для списка (None — общая тема / группа без тем).

    Сначала ищется тема именно для этого списка (/settopic ключ), затем общая (/settopic).
    Значение «0» означает «общая тема (General)».
    """
    value = db.get_setting(f"topic:{list_key}")
    if value is None:
        value = db.get_setting("topic:default")
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def _forget_topic(list_key: str) -> None:
    """Тема удалена или недоступна — возвращаем список в общую тему."""
    name = f"topic:{list_key}" if db.get_setting(f"topic:{list_key}") is not None else "topic:default"
    db.set_setting(name, "0")


def _paginate(title: str, lines: list) -> list:
    header = f"🖤 <b>{title}</b>\n{SEP}"
    pages, current, size = [], [header], len(header)
    for line in lines:
        if size + len(line) + 1 > PAGE_LIMIT and len(current) > 1:
            pages.append("\n".join(current))
            cont = f"🖤 <b>{title}</b> (продолжение)\n{SEP}"
            current, size = [cont], len(cont)
        current.append(line)
        size += len(line) + 1
    pages.append("\n".join(current))
    return pages


def _member_line(i, r) -> str:
    return f"{i}. <b>{esc(r['roblox_name'])}</b> — {tg_label(r['username'], r['full_name'])}"


def _staff_label(s) -> str:
    if s["username"]:
        tg = "@" + esc(s["username"])
    elif s["tg_id"]:
        tg = f"ID {s['tg_id']}"
    else:
        tg = "—"
    name = f"<b>{esc(s['roblox_name'])}</b> — " if s["roblox_name"] else ""
    pending = " <i>(ещё не запускал бота)</i>" if s["tg_id"] is None else ""
    return f"{name}{tg}{pending}"


def _staff_groups(staff, positions) -> list:
    lines = []
    for pos in positions:
        group = [s for s in staff if s["position"] == pos]
        if not group:
            continue
        lines.append(f"🔹 <b>{config.POSITIONS[pos]}</b> ({len(group)})")
        lines += [f"• {_staff_label(s)}" for s in group]
        lines.append("")
    return lines


def _owner_lines() -> list:
    return ["👑 <b>Владелец</b>", f"• @{esc(config.OWNER_USERNAME)}", ""]


def render(key: str) -> list:
    """Возвращает список страниц (текстов сообщений) для списка с ключом key."""
    if key == "all":
        rows = db.active_members()
        lines = [f"Активных участников: <b>{len(rows)}</b>", ""]
        for code in RANK_CODES:
            group = [r for r in rows if r["rank_code"] == code]
            lines.append(f"🔹 <b>{rank_label(code)}</b> ({len(group)})")
            lines += [_member_line(i, r) for i, r in enumerate(group, 1)] or ["—"]
            lines.append("")
        return _paginate("СОСТАВ DS", lines)

    if key in RANK_CODES:
        rows = db.active_members(rank=key)
        lines = [f"Участников: <b>{len(rows)}</b>", ""]
        lines += [_member_line(i, r) for i, r in enumerate(rows, 1)] or ["Пока никого нет."]
        return _paginate(rank_label(key).upper(), lines)

    staff = db.list_staff()
    if key == "staff":
        lines = _owner_lines() + _staff_groups(staff, config.POSITION_ORDER)
        if len(lines) == 3:
            lines.append("Других работников пока нет.")
        return _paginate("РАБОТНИКИ DS", lines)

    if key == "admins":
        lines = _owner_lines() + _staff_groups(staff, ["chief_admin", "admin"])
        return _paginate("АДМИНИСТРАЦИЯ DS", lines)

    if key == "checkers":
        perms = db.perms_map()
        checkers = [s for s in staff if "pvp_result" in perms.get(s["id"], set())]
        lines = [f"Проверяющих: <b>{len(checkers)}</b>", ""]
        lines += [f"• {_staff_label(s)} — {config.POSITIONS[s['position']]}" for s in checkers] \
            or ["Пока никого нет."]
        return _paginate("ПРОВЕРЯЮЩИЕ PvP", lines)

    raise KeyError(key)


class ListManager:
    def __init__(self):
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    def request_refresh(self) -> None:
        """Вызывается после любого изменения базы. Само обновление произойдёт в фоне."""
        self._event.set()

    async def worker(self, bot: Bot) -> None:
        while True:
            await self._event.wait()
            await asyncio.sleep(config.REFRESH_DEBOUNCE)  # объединяем серию изменений
            self._event.clear()
            try:
                await self.refresh_all(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Не удалось обновить списки, повторю через 30 секунд")
                await asyncio.sleep(30)
                self.request_refresh()

    async def reconcile_loop(self, bot: Bot) -> None:
        while True:
            await asyncio.sleep(config.RECONCILE_INTERVAL)
            try:
                await self.refresh_all(bot, force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка периодической сверки списков")

    async def refresh_all(self, bot: Bot, force: bool = False) -> bool:
        group_id = db.get_group_id()
        if not group_id:
            log.warning("Группа не привязана: отправьте /setgroup в группе клана (от имени владельца).")
            return False
        async with self._lock:
            for key in LIST_KEYS:
                await self._sync_key(bot, group_id, key, render(key), force)
        return True

    async def reset(self, bot: Bot, list_keys) -> None:
        """Удаляет опубликованные сообщения указанных списков; они создадутся заново (в новой теме)."""
        async with self._lock:
            for list_key in list_keys:
                for row in db.messages_with_prefix(f"{list_key}:"):
                    try:
                        await bot.delete_message(row["chat_id"], row["message_id"])
                    except TelegramAPIError:
                        pass
                    db.delete_message(row["key"])
        self.request_refresh()

    async def _sync_key(self, bot, chat_id, key, pages, force):
        for i, text in enumerate(pages):
            await self._upsert(bot, chat_id, f"{key}:{i}", text, force)
        for row in db.messages_with_prefix(f"{key}:"):
            if int(row["key"].split(":")[1]) >= len(pages):
                try:
                    await bot.delete_message(row["chat_id"], row["message_id"])
                except TelegramBadRequest:
                    pass
                db.delete_message(row["key"])

    async def _upsert(self, bot, chat_id, key, text, force):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = db.get_message(key)
        if row and row["chat_id"] == chat_id:
            if not force and row["text_hash"] == digest:
                return
            try:
                await self._call(bot.edit_message_text, text=text, chat_id=chat_id,
                                 message_id=row["message_id"])
                db.set_message(key, chat_id, row["message_id"], digest)
                return
            except TelegramBadRequest as exc:
                message = str(exc).lower()
                if "not modified" in message:
                    db.set_message(key, chat_id, row["message_id"], digest)
                    return
                gone = ("message to edit not found", "message can't be edited", "message_id_invalid")
                if not any(s in message for s in gone):
                    raise
                log.info("Сообщение %s удалено вручную — создаю заново", key)
        list_key = key.split(":")[0]
        thread_id = topic_for(list_key)
        params = {"chat_id": chat_id, "text": text, "disable_notification": True}
        if thread_id:
            params["message_thread_id"] = thread_id
        try:
            sent = await self._call(bot.send_message, **params)
        except TelegramBadRequest as exc:
            if not thread_id or "thread not found" not in str(exc).lower():
                raise
            log.warning("Тема %s не найдена (удалена?) — публикую список %s в общей теме", thread_id, list_key)
            _forget_topic(list_key)
            params.pop("message_thread_id")
            sent = await self._call(bot.send_message, **params)
        db.set_message(key, chat_id, sent.message_id, digest)

    @staticmethod
    async def _call(func, **kwargs):
        for _ in range(3):
            try:
                return await func(**kwargs)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
        return await func(**kwargs)


lists = ListManager()
