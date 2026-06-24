import asyncio
import logging
import os
import time
from datetime import datetime

import json

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, FSInputFile
)

import database as db
from config import ADMIN_ID

logger = logging.getLogger(__name__)

def _build_album(media_id_json, caption):
    """Из JSON-списка {type, file_id} собирает список InputMedia* для send_media_group.
    Подпись ставится только на первый элемент."""
    items = json.loads(media_id_json)
    media = []
    for i, it in enumerate(items):
        cap = caption or None if i == 0 else None
        if it["type"] == "photo":
            media.append(InputMediaPhoto(media=it["file_id"], caption=cap))
        elif it["type"] == "video":
            media.append(InputMediaVideo(media=it["file_id"], caption=cap))
        elif it["type"] == "document":
            media.append(InputMediaDocument(media=it["file_id"], caption=cap))
    return media

async def backup_task(bot: Bot, interval_hours: int = 24):
    """Раз в interval_hours делает копию БД и шлёт её админу в личку."""
    # первый бэкап — через час после старта, чтобы не спамить при перезапусках
    await asyncio.sleep(3600)
    while True:
        try:
            # автоочистка старых завершённых постов
            try:
                removed = await db.delete_old_posts(days=30)
                if removed:
                    logger.info("Автоочистка постов: удалено %s старых записей", removed)
            except Exception as e:
                logger.warning("Автоочистка постов не удалась: %s", e)

            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            backup_path = f"backup_{stamp}.db"
            await db.make_backup(backup_path)
            try:
                doc = FSInputFile(backup_path, filename=f"bot_backup_{stamp}.db")
                await bot.send_document(
                    ADMIN_ID, doc,
                    caption=f"💾 Резервная копия базы от {stamp}."
                )
            finally:
                # удаляем временный файл копии в любом случае
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
        except Exception as e:
            logger.error("Не удалось сделать бэкап БД: %s", e)
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Ошибка бэкапа БД: {e}")
            except Exception:
                pass
        await asyncio.sleep(interval_hours * 3600)

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
                    sent_id = None
                    if media_type == "photo":
                        m = await bot.send_photo(chat_id, media_id, caption=text or None,
                                                 reply_markup=keyboard)
                        sent_id = m.message_id
                    elif media_type == "video":
                        m = await bot.send_video(chat_id, media_id, caption=text or None,
                                                 reply_markup=keyboard)
                        sent_id = m.message_id
                    elif media_type == "document":
                        m = await bot.send_document(chat_id, media_id, caption=text or None,
                                                    reply_markup=keyboard)
                        sent_id = m.message_id
                    elif media_type == "album":
                        media = _build_album(media_id, text)
                        msgs = await bot.send_media_group(chat_id, media)
                        # подпись альбома живёт в первом сообщении
                        if msgs:
                            sent_id = msgs[0].message_id
                        # У альбома не бывает инлайн-кнопки — шлём её отдельным сообщением
                        if keyboard:
                            await bot.send_message(chat_id, text or "⬆️",
                                                   reply_markup=keyboard)
                    else:
                        m = await bot.send_message(chat_id, text, reply_markup=keyboard)
                        sent_id = m.message_id

                    # Запоминаем id опубликованного сообщения (для редактирования)
                    if sent_id is not None:
                        await db.set_sent_message_id(post_id, sent_id)

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
