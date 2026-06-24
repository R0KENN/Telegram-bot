from datetime import datetime

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db
from config import TZ


async def chats_kb():
    rows = []
    for chat_id, title, ctype in await db.get_chats():
        if chat_id == 0:
            continue  # служебная запись лог-группы — не показываем в списке
        icon = "📢" if ctype == "channel" else "👥"
        rows.append([InlineKeyboardButton(text=f"{icon} {title}", callback_data=f"ch:{chat_id}")])
    rows.append([InlineKeyboardButton(text="📋 Статус (сводка)", callback_data="globalstatus")])
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
            [InlineKeyboardButton(text="📮 Статистика постов", callback_data=f"poststats:{chat_id}")],
            [InlineKeyboardButton(text="🔥 Реакции", callback_data=f"reactions:{chat_id}")],
        ]
    else:  # группа
        rows += [
            [InlineKeyboardButton(text="🛡 Модерация", callback_data=f"mod:{chat_id}")],
            [InlineKeyboardButton(text="👋 Приветствие в группе", callback_data=f"gw:{chat_id}")],
            [InlineKeyboardButton(text="🤖 Капча для новичков", callback_data=f"captcha:{chat_id}")],
        ]
    rows += [
        [InlineKeyboardButton(text="📈 График прироста", callback_data=f"chart:{chat_id}"),
         InlineKeyboardButton(text="📄 Экспорт CSV", callback_data=f"export:{chat_id}")],
        [InlineKeyboardButton(text="🧵 Тема для логов", callback_data=f"logtopic:{chat_id}")],
        [InlineKeyboardButton(text="🔍 Проверить права", callback_data=f"rights:{chat_id}")],
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
    clean_srv = await db.get_setting(chat_id, "clean_service") == "1"
    t3 = "🟢 Чистка системных: ВКЛ" if clean_srv else "🔴 Чистка системных: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t1, callback_data=f"tdl:{chat_id}")],
        [InlineKeyboardButton(text="🌐 Разрешённые домены", callback_data=f"domains:{chat_id}")],
        [InlineKeyboardButton(text=t2, callback_data=f"twf:{chat_id}")],
        [InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data=f"words:{chat_id}")],
        [InlineKeyboardButton(text=t3, callback_data=f"tcs:{chat_id}")],
        [InlineKeyboardButton(text="⚠️ Предупреждения", callback_data=f"warns_menu:{chat_id}")],
        [InlineKeyboardButton(text="🌊 Антифлуд", callback_data=f"flood_menu:{chat_id}")],
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
# --- посты ---
async def posts_menu_kb(chat_id):
    rows = [[InlineKeyboardButton(text="➕ Новый отложенный пост", callback_data=f"newpost:{chat_id}")]]
    media_icons = {"photo": "🖼", "video": "🎬", "document": "📎", "album": "🗂"}
    repeat_icons = {"daily": "📅", "weekly": "🗓"}
    for pid, text, publish_at, media_type, repeat_mode in await db.get_pending_posts(chat_id):
        when = datetime.fromtimestamp(publish_at, TZ).strftime("%d.%m %H:%M")
        base = text or ""
        preview = (base[:18] + "…") if len(base) > 18 else (base or "медиа")
        micon = media_icons.get(media_type, "📝")
        ricon = repeat_icons.get(repeat_mode, "")
        rows.append([InlineKeyboardButton(
            text=f"{ricon}{micon} {when} | {preview}".replace("\n", " "),
            callback_data=f"postcard:{chat_id}:{pid}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def post_card_kb(chat_id, post_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"editposttext:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="🕓 Изменить время", callback_data=f"editposttime:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="🔁 Изменить повтор", callback_data=f"editpostrep:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="🔘 Изменить кнопку", callback_data=f"editpostbtn:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"delpost:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"posts:{chat_id}")],
    ])


def post_repeat_edit_kb(chat_id, post_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Разово", callback_data=f"setpostrep:{chat_id}:{post_id}:once")],
        [InlineKeyboardButton(text="📅 Каждый день", callback_data=f"setpostrep:{chat_id}:{post_id}:daily")],
        [InlineKeyboardButton(text="🗓 Каждую неделю", callback_data=f"setpostrep:{chat_id}:{post_id}:weekly")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"postcard:{chat_id}:{post_id}")],
    ])

async def editpub_list_kb(chat_id):
    rows = []
    media_icons = {"photo": "🖼", "video": "🎬", "document": "📎", "album": "🗂"}
    for pid, text, publish_at, media_type, sent_id in await db.get_published_posts(chat_id):
        when = datetime.fromtimestamp(publish_at, TZ).strftime("%d.%m %H:%M")
        base = text or ""
        preview = (base[:16] + "…") if len(base) > 16 else (base or "медиа")
        micon = media_icons.get(media_type, "📝")
        rows.append([InlineKeyboardButton(
            text=f"{micon} {when} | {preview}".replace("\n", " "),
            callback_data=f"editpubcard:{chat_id}:{pid}"
        )])
    if not rows:
        rows.append([InlineKeyboardButton(
            text="Нет опубликованных постов", callback_data=f"poststats:{chat_id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"poststats:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def editpub_card_kb(chat_id, post_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст/подпись", callback_data=f"editpubtext:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"editpub:{chat_id}")],
    ])

def post_delete_confirm_kb(chat_id, post_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delpostok:{chat_id}:{post_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"postcard:{chat_id}:{post_id}")],
    ])

def skip_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без кнопки", callback_data=f"nobtn:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"posts:{chat_id}")],
    ])

def poll_skip_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без опроса", callback_data=f"nopoll:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"posts:{chat_id}")],
    ])

def poll_options_kb(chat_id, is_anonymous=True, multiple=False):
    anon_label = "👁 Анонимный: ВКЛ" if is_anonymous else "👁 Анонимный: ВЫКЛ"
    multi_label = "☑️ Несколько ответов: ВКЛ" if multiple else "☑️ Несколько ответов: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=anon_label, callback_data=f"polltgl:anon:{chat_id}")],
        [InlineKeyboardButton(text=multi_label, callback_data=f"polltgl:multi:{chat_id}")],
        [InlineKeyboardButton(text="✅ Готово", callback_data=f"polldone:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"posts:{chat_id}")],
    ])

def repeat_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Разово", callback_data=f"setrepeat:{chat_id}:once")],
        [InlineKeyboardButton(text="📅 Каждый день", callback_data=f"setrepeat:{chat_id}:daily")],
        [InlineKeyboardButton(text="🗓 Каждую неделю", callback_data=f"setrepeat:{chat_id}:weekly")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"posts:{chat_id}")],
    ])

def post_confirm_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Запланировать", callback_data=f"postsave:{chat_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"postcancel:{chat_id}")],
    ])

def poststats_kb(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 История постов", callback_data=f"posthist:{chat_id}")],
        [InlineKeyboardButton(text="✏️ Редактировать опубликованные", callback_data=f"editpub:{chat_id}")],
        [InlineKeyboardButton(text="🧹 Очистить завершённые", callback_data=f"postclean:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])


async def post_history_kb(chat_id):
    rows = []
    status_icons = {"published": "✅", "cancelled": "🚫", "failed": "❌"}
    media_icons = {"photo": "🖼", "video": "🎬", "document": "📎", "album": "🗂"}
    for pid, text, publish_at, status, media_type, repeat_mode in await db.get_finished_posts(chat_id):
        when = datetime.fromtimestamp(publish_at, TZ).strftime("%d.%m %H:%M")
        base = text or ""
        preview = (base[:16] + "…") if len(base) > 16 else (base or "медиа")
        sicon = status_icons.get(status, "•")
        micon = media_icons.get(media_type, "📝")
        rows.append([InlineKeyboardButton(
            text=f"{sicon}{micon} {when} | {preview}".replace("\n", " "),
            callback_data=f"histcard:{chat_id}:{pid}"
        )])
    if not rows:
        rows.append([InlineKeyboardButton(text="История пуста", callback_data=f"poststats:{chat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"poststats:{chat_id}")])
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
        if thread_id:
            rows.append([InlineKeyboardButton(
                text="🗑 Удалить тему этого чата", callback_data=f"deltopicchat:{chat_id}"
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
def global_status_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить права во всех чатах", callback_data="rightsall")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="chats")],
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

# --- капча для новичков ---
async def captcha_menu_kb(chat_id):
    enabled = await db.get_setting(chat_id, "captcha_enabled") == "1"
    toggle = "🟢 Капча: ВКЛ" if enabled else "🔴 Капча: ВЫКЛ"
    timeout = await db.get_setting(chat_id, "captcha_timeout")
    action = await db.get_setting(chat_id, "captcha_action")
    action_label = "кик" if action == "kick" else "без права писать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"captoggle:{chat_id}")],
        [InlineKeyboardButton(text=f"⏱ Время: {timeout} сек", callback_data=f"captime:{chat_id}")],
        [InlineKeyboardButton(text=f"⚙️ Не прошёл → {action_label}", callback_data=f"capaction:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ch:{chat_id}")],
    ])

# --- настройка предупреждений ---
async def warns_menu_kb(chat_id):
    mute_limit = await db.get_setting(chat_id, "warn_mute_limit")
    ban_limit = await db.get_setting(chat_id, "warn_ban_limit")
    mute_minutes = await db.get_setting(chat_id, "warn_mute_minutes")
    on_mod = await db.get_setting(chat_id, "warn_on_moderation") == "1"
    mod_toggle = "🟢 Варн при авто-модерации: ВКЛ" if on_mod else "🔴 Варн при авто-модерации: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔇 Мут после: {mute_limit} варн.", callback_data=f"wsetmute:{chat_id}")],
        [InlineKeyboardButton(text=f"🚫 Бан после: {ban_limit} варн.", callback_data=f"wsetban:{chat_id}")],
        [InlineKeyboardButton(text=f"⏱ Длительность мута: {mute_minutes} мин", callback_data=f"wsetminutes:{chat_id}")],
        [InlineKeyboardButton(text=mod_toggle, callback_data=f"wtogglemod:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mod:{chat_id}")],
    ])

# --- антифлуд ---
async def flood_menu_kb(chat_id):
    enabled = await db.get_setting(chat_id, "antiflood_enabled") == "1"
    toggle = "🟢 Антифлуд: ВКЛ" if enabled else "🔴 Антифлуд: ВЫКЛ"
    count = await db.get_setting(chat_id, "antiflood_count")
    window = await db.get_setting(chat_id, "antiflood_window")
    minutes = await db.get_setting(chat_id, "antiflood_mute_minutes")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"floodtoggle:{chat_id}")],
        [InlineKeyboardButton(text=f"📊 Лимит: {count} сообщ.", callback_data=f"floodcount:{chat_id}")],
        [InlineKeyboardButton(text=f"⏱ Окно: {window} сек", callback_data=f"floodwindow:{chat_id}")],
        [InlineKeyboardButton(text=f"🔇 Мут: {minutes} мин", callback_data=f"floodminutes:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mod:{chat_id}")],
    ])
