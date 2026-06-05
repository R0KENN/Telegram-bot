import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db
from config import ADMIN_ID

logger = logging.getLogger(__name__)


async def scheduler(bot: Bot):
    while True:
        try:
            for post_id, chat_id, text, btn_text, btn_url in await db.get_due_posts():
                keyboard = None
                if btn_text and btn_url:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=btn_text, url=btn_url)]
                    ])
                try:
                    await bot.send_message(chat_id, text, reply_markup=keyboard)
                    await db.mark_post(post_id, "published")
                    try:
                        await bot.send_message(ADMIN_ID, "✅ Опубликован отложенный пост.")
                    except Exception:
                        pass
                except Exception as e:
                    await db.mark_post(post_id, "failed")
                    logger.error(f"Не удалось опубликовать пост {post_id}: {e}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"⚠️ Не удалось опубликовать пост. Бот админ канала? Ошибка: {e}"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Ошибка планировщика: {e}")
        await asyncio.sleep(20)
