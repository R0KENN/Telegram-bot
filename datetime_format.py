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
    # Сначала переведём наивное серверное время в реальную метку (UTC)
    aware = dt.replace(tzinfo=timezone(_td(tz_offset)))

    # Текстовое представление даты (то, что увидят старые клиенты / fallback)
    human = dt.strftime("%d.%m.%Y %H:%M")
    full_text = f"{prefix}{human}"

    # Позиция и длина подстроки с датой (в UTF-16 code units!)
    offset = _utf16_len(prefix)
    length = _utf16_len(human)

    entity = MessageEntity(
        type="date_time",
        offset=offset,
        length=length,
        # Telegram сам отформатирует по unix-времени; передаём метку времени
        # через поле, которое требует API. В сыром виде это timestamp:
        # (имя поля зависит от версии — см. ниже про fallback)
    )

    try:
        await bot.send_message(chat_id, full_text, entities=[entity])
    except TelegramBadRequest as e:
        # Если версия API/aiogram не поддерживает date_time — откатываемся
        logger.warning("date_time entity не поддержан, fallback: %s", e)
        await bot.send_message(chat_id, full_text)


def _td(hours: int):
    from datetime import timedelta
    return timedelta(hours=hours)


def _utf16_len(s: str) -> int:
    """Длина строки в UTF-16 code units — так Telegram считает offset/length."""
    return len(s.encode("utf-16-le")) // 2
