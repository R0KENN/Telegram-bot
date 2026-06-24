import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
import database as db
from handlers import router
from scheduler import scheduler, backup_task
from commands import setup_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),                       # вывод в консоль
        logging.FileHandler("bot.log", encoding="utf-8"),  # дублирование в файл
    ],
)
# Понижаем «болтливость» библиотек, чтобы лог не был засорён
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=False)
    await setup_commands(bot)
    asyncio.create_task(scheduler(bot))
    asyncio.create_task(backup_task(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
