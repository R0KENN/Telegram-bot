from datetime import datetime

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db
from config import TZ


async def chats_kb():
    rows = []
    for chat_id, title, ctype in await db.get_chats():
        icon = "📢" if ctype == "channel" else "👥"
        rows.append([InlineKeyboardButton(text=f"{icon} {title}", callback_data=f"ch:{chat_id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить канал/группу", callback_data="addch")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def chat_menu_kb(chat_id):
    chat = await db.get_chat(chat_id)
    ctype = chat[2] if chat else "channel"
    auto_on = await db.is_auto_approve(chat_id)
    toggle = "🟢 Автоприём: ВКЛ" if auto_on else "🔴 Автоприём: ВЫКЛ"
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats:{chat_id}"),
         InlineKeyboardButton(text="🕓 Последние", callback_data=f"last:{chat_id}")],
        [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{chat_id}")],
    ]
    if ctype == "channel":
        rows += [
            [InlineKeyboardButton(text="⚙️ Приветствие (в личку)", callback_data=f"wmenu:{chat_id}")],
            [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"bc:{chat_id}")],
            [InlineKeyboardButton(text="📝 Посты", callback_data=f"posts:{chat_id}")],
            [InlineKeyboardButton(text="🔥 Реакции", callback_data=f"reactions:{chat_id}")],
        ]
    else:  # группа
        rows += [
            [InlineKeyboardButton(text="🛡 Модерация", callback_data=f"mod:{chat_id}")],
            [InlineKeyboardButton(text="👋 Приветствие в группе", callback_data=f"gw:{chat_id}")],
        ]
    rows += [
        [InlineKeyboardButton(text="🧵 Темы для уведомлений", callback_data=f"routes:{chat_id}")],
        [InlineKeyboardButton(text="🧵 Тема для логов", callback_data=f"logtopic:{chat_id}")],
        [InlineKeyboardButton(text="♻️ Сбросить настройки", callback_data=f"reset:{chat_id}")],
        [InlineKeyboardButton(text="🗑 Удалить из списка", callback_data=f"delchat:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="chats")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- приветствие в личку (каналы) ---
async def welcome_menu_kb(chat_id):
    enabled = await db.get_setting(chat_id, "welcome_enabled") == "1"
    toggle = "🟢 Приветствие: ВКЛ" if enabled else "🔴 Приветствие: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"wtoggle:{chat_id}")],
        [InlineKeyboardButton(text="✏️ Текст", callback_data=f"st:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Задержка", callback_data=f"sd:{chat_id}")],
        [InlineKeyboardButton(text="🔘 Кнопки", callback_data=f"wbtns:{chat_id}")],
        [InlineKeyboardButton(text="👁 Проверить", callback_data=f"show:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])


async def welcome_buttons_kb(chat_id):
    rows = []
    for bid, text, url in await db.get_welcome_buttons(chat_id):
        rows.append([InlineKeyboardButton(
            text=f"🗑 {text}", callback_data=f"delwbtn:{chat_id}:{bid}"
        )])
    buttons = await db.get_welcome_buttons(chat_id)
    if len(buttons) < 4:
        rows.append([InlineKeyboardButton(text="➕ Добавить кнопку", callback_data=f"addwbtn:{chat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"wmenu:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- модерация групп ---
async def mod_menu_kb(chat_id):
    del_links = await db.get_setting(chat_id, "del_links") == "1"
    word_filter = await db.get_setting(chat_id, "word_filter") == "1"
    t1 = "🟢 Удалять ссылки: ВКЛ" if del_links else "🔴 Удалять ссылки: ВЫКЛ"
    t2 = "🟢 Фильтр слов: ВКЛ" if word_filter else "🔴 Фильтр слов: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t1, callback_data=f"tdl:{chat_id}")],
        [InlineKeyboardButton(text="🌐 Разрешённые домены", callback_data=f"domains:{chat_id}")],
        [InlineKeyboardButton(text=t2, callback_data=f"twf:{chat_id}")],
        [InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data=f"words:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])


async def domains_kb(chat_id):
    rows = []
    for did, domain in await db.get_allowed_domains(chat_id):
        rows.append([InlineKeyboardButton(text=f"🗑 {domain}", callback_data=f"deldom:{chat_id}:{did}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить домен", callback_data=f"adddom:{chat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mod:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def words_kb(chat_id):
    rows = []
    for wid, word in await db.get_banned_words(chat_id):
        rows.append([InlineKeyboardButton(text=f"🗑 {word}", callback_data=f"delword:{chat_id}:{wid}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data=f"addword:{chat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mod:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- приветствие в группе ---
async def group_welcome_kb(chat_id):
    enabled = await db.get_setting(chat_id, "group_welcome_enabled") == "1"
    toggle = "🟢 Приветствие: ВКЛ" if enabled else "🔴 Приветствие: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"gwtoggle:{chat_id}")],
        [InlineKeyboardButton(text="✏️ Текст", callback_data=f"gwtext:{chat_id}")],
        [InlineKeyboardButton(text="⏱ Автоудаление (сек)", callback_data=f"gwttl:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])


# --- посты ---
async def posts_menu_kb(chat_id):
    rows = [[InlineKeyboardButton(text="➕ Новый отложенный пост", callback_data=f"newpost:{chat_id}")]]
    for pid, text, publish_at in await db.get_pending_posts(chat_id):
        when = datetime.fromtimestamp(publish_at, TZ).strftime("%d.%m %H:%M")
        preview = (text[:20] + "…") if len(text) > 20 else text
        rows.append([InlineKeyboardButton(
            text=f"🗑 {when} | {preview}".replace("\n", " "),
            callback_data=f"delpost:{chat_id}:{pid}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без кнопки", callback_data=f"nobtn:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"posts:{chat_id}")],
    ])

# --- маршрутизация по темам ---
# Список событий, которые можно раскидать по темам
TOPIC_EVENTS = [
    ("join", "📥 Заявки"),
    ("newmember", "➕ Новые участники"),
    ("report", "🛡 Модерация/отчёты"),
]


async def topics_route_kb(chat_id):
    log_chat = await db.get_log_chat(chat_id)
    rows = []
    if not log_chat:
        rows.append([InlineKeyboardButton(
            text="📌 Сначала выбери лог-группу", callback_data=f"setloggrp:{chat_id}"
        )])
    else:
        for event_key, label in TOPIC_EVENTS:
            thread_id = await db.get_topic_route(chat_id, event_key)
            name = "— не задано"
            if thread_id:
                for _id, t_id, t_name in await db.get_topics(log_chat):
                    if t_id == thread_id:
                        name = t_name
                        break
            rows.append([InlineKeyboardButton(
                text=f"{label}: {name}",
                callback_data=f"pickroute:{chat_id}:{event_key}"
            )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def topic_choice_kb(chat_id, event_key, log_chat):
    """Список тем лог-группы для выбора под конкретное событие."""
    rows = []
    for _id, thread_id, name in await db.get_topics(log_chat):
        rows.append([InlineKeyboardButton(
            text=name, callback_data=f"setroute:{chat_id}:{event_key}:{thread_id}"
        )])
    if not rows:
        rows.append([InlineKeyboardButton(
            text="В лог-группе нет тем (создай /newtopic)", callback_data=f"routes:{chat_id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"routes:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# --- привязка темы лог-группы к чату ---
async def log_topic_kb(chat_id):
    log_chat = await db.get_global_log_chat()
    rows = []
    if not log_chat:
        rows.append([InlineKeyboardButton(
            text="📌 Сначала задать лог-группу", callback_data=f"setlogchat:{chat_id}"
        )])
    else:
        thread_id = await db.get_log_thread(chat_id)
        cur_name = "— не задано"
        if thread_id:
            for _id, t_id, t_name in await db.get_topics(log_chat):
                if t_id == thread_id:
                    cur_name = t_name
                    break
        rows.append([InlineKeyboardButton(
            text=f"Текущая тема: {cur_name}", callback_data=f"logtopic:{chat_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="📋 Выбрать тему", callback_data=f"picklogtopic:{chat_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="⚙️ Создать темы для всех чатов", callback_data=f"autotopics:{chat_id}"
        )])
        disabled = await db.get_setting(chat_id, "log_disabled") == "1"
        dtoggle = "🔴 Логи в тему: ВЫКЛ" if disabled else "🟢 Логи в тему: ВКЛ"
        rows.append([InlineKeyboardButton(
            text=dtoggle, callback_data=f"logtoggle:{chat_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="🔄 Сменить лог-группу", callback_data=f"setlogchat:{chat_id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def log_topic_choice_kb(chat_id, log_chat):
    rows = []
    for _id, thread_id, name in await db.get_topics(log_chat):
        rows.append([InlineKeyboardButton(
            text=name, callback_data=f"setlogtopic:{chat_id}:{thread_id}"
        )])
    if not rows:
        rows.append([InlineKeyboardButton(
            text="В лог-группе нет тем — создай /newtopic", callback_data=f"logtopic:{chat_id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"logtopic:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb(target):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
    ])
# Популярные разрешённые эмодзи для выбора
REACTION_CHOICES = ["👍", "❤", "🔥", "🎉", "👏", "😁", "🤩", "🙏", "👌", "🤝", "💯", "⚡"]


async def reaction_pick_kb(chat_id):
    current = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    rows, line = [], []
    for emoji in REACTION_CHOICES:
        mark = "✅" if emoji == current else ""
        line.append(InlineKeyboardButton(
            text=f"{mark}{emoji}", callback_data=f"setreact:{chat_id}:{emoji}"
        ))
        if len(line) == 4:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"reactions:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Варианты задержки: подпись -> секунды
REACTION_DELAYS = [
    ("Сразу", 0),
    ("1 мин", 60),
    ("3 мин", 180),
    ("5 мин", 300),
    ("10 мин", 600),
    ("30 мин", 1800),
]


async def reaction_delay_kb(chat_id):
    current = await db.get_setting(chat_id, "reaction_delay") or "180"
    rows, line = [], []
    for label, secs in REACTION_DELAYS:
        mark = "✅ " if str(secs) == current else ""
        line.append(InlineKeyboardButton(
            text=f"{mark}{label}", callback_data=f"setdelay:{chat_id}:{secs}"
        ))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"reactions:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def reactions_menu_kb(chat_id):
    react_on = await db.get_setting(chat_id, "auto_reaction") == "1"
    toggle = "🟢 Авто-реакция: ВКЛ" if react_on else "🔴 Авто-реакция: ВЫКЛ"
    emoji = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    delay = int(await db.get_setting(chat_id, "reaction_delay") or "180")
    delay_label = "сразу" if delay == 0 else (f"{delay // 60} мин" if delay >= 60 else f"{delay} сек")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"treact:{chat_id}")],
        [InlineKeyboardButton(text=f"🎯 Эмодзи: {emoji}", callback_data=f"pickreact:{chat_id}")],
        [InlineKeyboardButton(text=f"⏱ Задержка: {delay_label}", callback_data=f"pickdelay:{chat_id}")],
        [InlineKeyboardButton(text="🔁 Реакции на старые посты", callback_data=f"reactall:{chat_id}")],
        [InlineKeyboardButton(text="🧹 Снять все реакции бота", callback_data=f"clearall:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])
