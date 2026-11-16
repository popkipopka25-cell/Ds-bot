"""DS Bot 2.0 — запуск и регистрация обработчиков (версия для Render + Neon PostgreSQL)."""
import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, FSInputFile, Message
from aiohttp import web

import admin
import config
import members_system
import pvp_system
import staff_system
import topics
from database import db
from message_manager import lists

log = logging.getLogger("ds_bot")


class TouchMiddleware(BaseMiddleware):
    """Обновляет @username в базе, привязывает работников, добавленных по @username,
    и сбрасывает диалог (FSM), если пользователь отправил команду."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            try:
                if db.touch_identity(user.id, user.username, user.full_name):
                    lists.request_refresh()
                    await staff_system.apply_commands(data["bot"], user.id)
            except Exception:
                log.exception("touch_identity")
            if isinstance(event, Message) and event.text and event.text.startswith("/"):
                state = data.get("state")
                if state:
                    await state.clear()
        return await handler(event, data)


# ------------------------------------------------------------------ веб-сервер (Render + UptimeRobot)
async def _health(_request: web.Request) -> web.Response:
    # Намеренно НЕ обращается к базе: частые пинги UptimeRobot не должны будить Neon.
    return web.Response(text="DS Bot is running")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)        # GET и HEAD (UptimeRobot)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.PORT).start()
    log.info("Веб-сервер для мониторинга слушает порт %s", config.PORT)
    return runner


# ------------------------------------------------------------------ резервные копии
async def backup_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(config.BACKUP_INTERVAL)
        try:
            path = await asyncio.to_thread(db.backup)
            log.info("Резервная копия создана: %s", path.name)
            if config.BACKUP_SEND_TO_OWNER and config.OWNER_ID:
                await bot.send_document(config.OWNER_ID, FSInputFile(path),
                                        caption="💾 Ежедневная резервная копия базы DS", disable_notification=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось создать или отправить резервную копию")


def setup_logging() -> None:
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(config.BASE_DIR / "ds_bot.log", maxBytes=1_000_000,
                                            backupCount=3, encoding="utf-8"))
    except OSError:
        pass  # на хостинге с read-only диском хватит вывода в консоль (логи Render)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


async def main() -> None:
    setup_logging()
    config.validate()

    # Сначала открываем порт — Render должен сразу увидеть, что сервис «живой».
    web_runner = await start_web_server()

    # Подключаемся к Neon и создаём таблицы (в потоке, чтобы не блокировать запуск).
    await asyncio.to_thread(db.connect)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                             link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(TouchMiddleware())
    dp.callback_query.outer_middleware(TouchMiddleware())

    # Порядок важен: сначала меню работников/PvP/админки, затем участники и группа.
    dp.include_router(staff_system.router)
    dp.include_router(pvp_system.router)
    dp.include_router(admin.router)
    dp.include_router(members_system.router)
    dp.include_router(members_system.group_router)
    dp.include_router(topics.router)

    @dp.error()
    async def on_error(event: ErrorEvent):
        log.exception("Ошибка в обработчике", exc_info=event.exception)
        update = event.update
        try:
            if update.message:
                await update.message.answer("⚠️ Произошла ошибка. Попробуй ещё раз чуть позже.")
            elif update.callback_query:
                await update.callback_query.answer("⚠️ Произошла ошибка. Попробуй позже.", show_alert=True)
        except TelegramAPIError:
            pass
        return True

    tasks = []
    try:
        me = await bot.get_me()
        log.info("Бот запущен: @%s", me.username)
        group_id = db.get_group_id()
        if group_id:
            try:
                member = await bot.get_chat_member(group_id, me.id)
                if member.status != "administrator":
                    log.warning("Бот НЕ администратор группы %s — выдайте права администратора.", group_id)
            except TelegramAPIError as exc:
                log.warning("Не удалось проверить права бота в группе: %s", exc)
        else:
            log.warning("Группа не привязана. Напишите /setgroup в группе клана (от имени владельца).")

        await bot.delete_webhook(drop_pending_updates=False)
        await staff_system.setup_default_commands(bot)

        tasks = [
            asyncio.create_task(lists.worker(bot), name="lists-worker"),
            asyncio.create_task(lists.reconcile_loop(bot), name="lists-reconcile"),
            asyncio.create_task(members_system.periodic_sync(bot), name="members-sync"),
            asyncio.create_task(backup_loop(bot), name="backup"),
        ]
        lists.request_refresh()  # при старте сверяем опубликованные списки с базой

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()
        await web_runner.cleanup()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code)
