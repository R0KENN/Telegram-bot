import logging
from aiogram import Bot, Router
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

import database as db

logger = logging.getLogger(__name__)
router = Router(name="topics")


async def create_topic(bot: Bot, chat_id: int, name: str,
                       icon_color: int | None = None,
                       icon_emoji_id: str | None = None) -> int | None:
    """Создаёт тему и возвращает message_thread_id (или None при ошибке)."""
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=name,
            icon_color=icon_color,            # напр. 0x6FB9F0
            icon_custom_emoji_id=icon_emoji_id,
        )
        # сохраняем в БД, чтобы потом писать в эту тему
        await db.add_topic(chat_id, topic.message_thread_id, name)
        return topic.message_thread_id
    except TelegramBadRequest as e:
        logger.error("Не удалось создать тему: %s", e)
        return None


async def send_to_topic(bot: Bot, chat_id: int, thread_id: int, text: str):
    """Отправить сообщение в конкретную тему."""
    await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text)


# Команда для создания темы (только для админов — проверку прав добавь сам)
@router.message(lambda m: m.text and m.text.startswith("/newtopic"))
async def cmd_new_topic(message: Message, bot: Bot):
    name = message.text.removeprefix("/newtopic").strip()
    if not name:
        await message.reply("Использование: /newtopic Название темы")
        return
    thread_id = await create_topic(bot, message.chat.id, name)
    if thread_id:
        await send_to_topic(bot, message.chat.id, thread_id, f"Тема «{name}» создана 🎉")
    else:
        await message.reply("Не удалось создать тему. Проверь, что включён режим тем и у бота есть право управлять темами.")
