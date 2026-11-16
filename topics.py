"""Темы форума: в какую тему группы бот публикует списки состава.

Команды (только владелец, писать прямо в нужной теме группы клана):
  /settopic            — все списки в этой теме
  /settopic ключ       — только один список в этой теме (all, DSa, DSb…, staff, admins, checkers)
  /cleartopic          — вернуть все списки в общую тему
  /cleartopic ключ     — вернуть один список к общей теме
Настройки хранятся в Neon и переживают перезапуски.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from database import db
from message_manager import LIST_KEYS, lists
from staff_system import is_owner
from utils import actor_name

log = logging.getLogger(__name__)

router = Router(name="topics")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

_KEYS = {k.lower(): k for k in LIST_KEYS}
KEYS_HELP = ", ".join(LIST_KEYS)


async def _check(msg: Message) -> bool:
    if not msg.from_user or not is_owner(msg.from_user.id):
        return False
    if msg.chat.id != db.get_group_id():
        await msg.reply("Сначала привяжи группу: отправь /setgroup в ней.")
        return False
    return True


def _parse_key(command: CommandObject):
    """Возвращает (ключ или None, ошибка или None)."""
    arg = (command.args or "").strip().lower()
    if not arg:
        return None, None
    if arg not in _KEYS:
        return None, f"Неизвестный список «{arg}». Доступно: {KEYS_HELP}"
    return _KEYS[arg], None


@router.message(Command("settopic"))
async def cmd_settopic(msg: Message, command: CommandObject):
    if not await _check(msg):
        return
    key, error = _parse_key(command)
    if error:
        return await msg.reply(error)
    thread_id = msg.message_thread_id if msg.is_topic_message else 0  # 0 — общая тема (General)
    if key:
        db.set_setting(f"topic:{key}", thread_id)
        affected = [key]
        where = f"список «{key}»"
    else:
        db.set_setting("topic:default", thread_id)
        affected = [k for k in LIST_KEYS if db.get_setting(f"topic:{k}") is None]
        where = "все списки"
    db.log(msg.from_user.id, actor_name(msg.from_user), "settings",
           f"Тема для списков ({key or 'все'}): {thread_id or 'общая'}")
    await lists.reset(msg.bot, affected)
    place = "в этой теме" if thread_id else "в общей теме"
    await msg.reply(f"✅ Теперь {where} будет публиковаться {place}. Старые сообщения перенесу автоматически.")


@router.message(Command("cleartopic"))
async def cmd_cleartopic(msg: Message, command: CommandObject):
    if not await _check(msg):
        return
    key, error = _parse_key(command)
    if error:
        return await msg.reply(error)
    if key:
        db.run("DELETE FROM settings WHERE key=%s", (f"topic:{key}",))
        affected = [key]
    else:
        db.run("DELETE FROM settings WHERE key LIKE %s", ("topic:%",))
        affected = list(LIST_KEYS)
    db.log(msg.from_user.id, actor_name(msg.from_user), "settings", f"Сброс темы списков: {key or 'все'}")
    await lists.reset(msg.bot, affected)
    await msg.reply("✅ Готово: списки возвращены в общую тему.")
