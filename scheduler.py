import asyncio
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db
from config import ADMIN_ID

logger = logging.getLogger(__name__)


async def scheduler(bot: Bot):
    while True:
        try:
            for (post_id, chat_id, text, btn_text, btn_url,
                 media_type, media_id, repeat_mode) in await db.get_due_posts():
                keyboard = None
                if btn_text and btn_url:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=btn_text, url=btn_url)]
                    ])
                try:
                    if media_type == "photo":
                        await bot.send_photo(chat_id, media_id, caption=text or None,
                                             reply_markup=keyboard)
                    elif media_type == "video":
                        await bot.send_video(chat_id, media_id, caption=text or None,
                                             reply_markup=keyboard)
                    elif media_type == "document":
                        await bot.send_document(chat_id, media_id, caption=text or None,
                                                reply_markup=keyboard)
                    else:
                        await bot.send_message(chat_id, text, reply_markup=keyboard)

                    # Повтор или завершение
                    if repeat_mode == "daily":
                        await db.reschedule_post(post_id, int(time.time()) + 86400)
                    elif repeat_mode == "weekly":
                        await db.reschedule_post(post_id, int(time.time()) + 7 * 86400)
                    else:
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
