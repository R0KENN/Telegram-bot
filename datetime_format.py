import logging
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.types import MessageEntity
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


async def send_with_localized_time(bot: Bot, chat_id: int, prefix: str, dt: datetime,
                                   tz_offset: int = 3) -> None:
    """
    Отправляет сообщение, где время показывается в локали каждого пользователя.
    dt — наивный datetime в часовом поясе сервера (как хранится у тебя сейчас).
    """
    # Приводим наивное серверное время к его реальному часовому поясу
    aware = dt.replace(tzinfo=timezone(_td(tz_offset)))

    # Bot API не поддерживает сущность типа "date_time",
    # поэтому просто форматируем дату текстом.
    human = aware.strftime("%d.%m.%Y %H:%M")
    full_text = f"{prefix}{human}"

    try:
        await bot.send_message(chat_id, full_text)
    except TelegramBadRequest as e:
        logger.warning("Не удалось отправить сообщение с датой: %s", e)


def _td(hours: int):
    from datetime import timedelta
    return timedelta(hours=hours)


def _utf16_len(s: str) -> int:
    """Длина строки в UTF-16 code units — так Telegram считает offset/length."""
    return len(s.encode("utf-16-le")) // 2
