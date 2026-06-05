from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db


async def channels_kb():
    rows = []
    for chat_id, title in await db.get_channels():
        rows.append([InlineKeyboardButton(text=f"📢 {title}", callback_data=f"ch:{chat_id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="addch")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def channel_menu_kb(chat_id):
    auto_on = await db.is_auto_approve(chat_id)
    toggle = "🟢 Автоприём: ВКЛ" if auto_on else "🔴 Автоприём: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats:{chat_id}"),
         InlineKeyboardButton(text="🕓 Последние", callback_data=f"last:{chat_id}")],
        [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{chat_id}")],
        [InlineKeyboardButton(text="⚙️ Приветствие", callback_data=f"wmenu:{chat_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"bc:{chat_id}")],
        [InlineKeyboardButton(text="📝 Посты", callback_data=f"posts:{chat_id}")],
        [InlineKeyboardButton(text="♻️ Сбросить настройки", callback_data=f"reset:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ К списку каналов", callback_data="channels")],
    ])


def welcome_menu_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Текст", callback_data=f"st:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Задержка", callback_data=f"sd:{chat_id}")],
        [InlineKeyboardButton(text="🔗 Ссылка на правила", callback_data=f"sr:{chat_id}")],
        [InlineKeyboardButton(text="👁 Проверить", callback_data=f"show:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])


async def posts_menu_kb(chat_id):
    from config import TZ
    from datetime import datetime
    rows = [[InlineKeyboardButton(text="➕ Новый отложенный пост",
                                  callback_data=f"newpost:{chat_id}")]]
    for pid, text, publish_at in await db.get_pending_posts(chat_id):
        when = datetime.fromtimestamp(publish_at, TZ).strftime("%d.%m %H:%M")
        preview = (text[:20] + "…") if len(text) > 20 else text
        preview = preview.replace("\n", " ")
        rows.append([InlineKeyboardButton(
            text=f"🗑 {when} | {preview}", callback_data=f"delpost:{chat_id}:{pid}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без кнопки", callback_data=f"nobtn:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"posts:{chat_id}")],
    ])


def back_kb(target):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
    ])
