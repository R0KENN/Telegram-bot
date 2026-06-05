import asyncio
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from aiogram import Router, F
from aiogram.types import (
    ChatJoinRequest, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated,
    ReactionTypeEmoji
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatType

import database as db
import keyboards as kb
from config import ADMIN_ID, TZ, TIMEZONE_OFFSET, BROADCAST_PAUSE

router = Router()

URL_RE = re.compile(r'(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+|@\w+)', re.IGNORECASE)


class Form(StatesGroup):
    welcome_text = State()
    welcome_delay = State()
    wbtn_text = State()
    wbtn_url = State()
    broadcast = State()
    post_text = State()
    post_button = State()
    post_time = State()
    gw_text = State()
    gw_ttl = State()
    domain = State()
    word = State()


def is_admin_id(uid):
    return uid == ADMIN_ID


# ============================================================
#                  ЛИЧНЫЕ СООБЩЕНИЯ / МЕНЮ
# ============================================================
@router.message(Command("start"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin_id(message.from_user.id):
        await message.answer("Бот для управления каналами и группами 🤖")
        return
    await message.answer("<b>Твои каналы и группы:</b>", reply_markup=await kb.chats_kb())


@router.callback_query(F.data == "chats")
async def cb_chats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("<b>Твои каналы и группы:</b>", reply_markup=await kb.chats_kb())
    await c.answer()


@router.callback_query(F.data == "addch")
async def cb_addch(c: CallbackQuery):
    await c.message.edit_text(
        "➕ <b>Добавление</b>\n\n"
        "<b>Канал:</b> добавь бота админом в канал и перешли мне любой пост оттуда.\n\n"
        "<b>Группа:</b> просто добавь бота админом в группу — она появится автоматически.\n\n"
        "Боту нужны права: «Добавлять участников», «Удалять сообщения», «Публикация».",
        reply_markup=kb.back_kb("chats")
    )
    await c.answer()


# Регистрация канала пересылкой
@router.message(F.forward_from_chat, F.chat.type == ChatType.PRIVATE)
async def on_forward(message: Message):
    if not is_admin_id(message.from_user.id):
        return
    chat = message.forward_from_chat
    if chat.type != "channel":
        await message.answer("Это не канал. Для групп просто добавь бота в группу.")
        return
    is_new = await db.register_chat(chat.id, chat.title or str(chat.id), "channel")
    note = "\n\n⚠️ Автоприём выключен по умолчанию — включи его в меню." if is_new else ""
    await message.answer(f"✅ Канал «{chat.title}» добавлен. Открой /menu.{note}")


# Регистрация группы при добавлении бота
@router.my_chat_member()
async def on_added_to_group(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    new_status = event.new_chat_member.status
    if new_status in ("administrator", "member"):
        is_new = await db.register_chat(chat.id, chat.title or str(chat.id), "group")
        if is_new:
            try:
                await event.bot.send_message(
                    ADMIN_ID,
                    f"✅ Бот добавлен в группу «{chat.title}».\n"
                    f"Открой /menu для настройки. Дай боту права админа "
                    f"(удаление сообщений) для модерации."
                )
            except Exception:
                pass


# ============================================================
#            АВТО-РЕАКЦИИ НА ПОСТЫ В КАНАЛЕ
# ============================================================

# Эмодзи, которым бот реагирует на каждый пост в канале.
CHANNEL_REACTION = "🔥"


@router.channel_post()
async def auto_react_channel_post(message: Message):
    # Реагируем только если для этого канала реакции включены в настройках.
    if await db.get_setting(message.chat.id, "auto_reaction") != "1":
        return
    emoji = await db.get_setting(message.chat.id, "reaction_emoji") or CHANNEL_REACTION
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        # Нет прав, эмодзи не разрешён в канале, сообщение удалено и т.п. — молча игнорируем.
        pass

# ============================================================
#                       ЗАЯВКИ
# ============================================================
@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest):
    bot = request.bot
    chat_id = request.chat.id
    ctype = "channel" if request.chat.type == "channel" else "group"
    await db.register_chat(chat_id, request.chat.title or str(chat_id), ctype)
    if not await db.is_auto_approve(chat_id):
        await notify_admin(bot, request, approved=False)
        return
    user = request.from_user
    try:
        await request.approve()
        await db.save_member(chat_id, user.id, user.full_name, user.username or "")
        await notify_admin(bot, request, approved=True)
        if ctype == "channel":
            asyncio.create_task(send_delayed_welcome(bot, chat_id, user.id))
    except Exception:
        pass


async def notify_admin(bot, request: ChatJoinRequest, approved: bool):
    user = request.from_user
    status = "✅ одобрена" if approved else "⏳ ожидает (автоприём выкл.)"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📥 Заявка в «{request.chat.title}» ({status})\n"
            f"{user.full_name} | @{user.username or '—'} | <code>{user.id}</code>"
        )
    except Exception:
        pass


async def send_delayed_welcome(bot, chat_id, user_id):
    if await db.get_setting(chat_id, "welcome_enabled") != "1":
        return
    delay = int(await db.get_setting(chat_id, "welcome_delay"))
    await asyncio.sleep(delay)
    text = await db.get_setting(chat_id, "welcome_text")
    buttons = await db.get_welcome_buttons(chat_id)
    keyboard = None
    if buttons:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, url=u)] for _, t, u in buttons
        ])
    try:
        await bot.send_message(user_id, text, reply_markup=keyboard)
    except Exception:
        pass


# ============================================================
#              МОДЕРАЦИЯ ГРУПП + ПРИВЕТСТВИЕ В ГРУППЕ
# ============================================================
async def is_user_admin(bot, chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


def extract_domains(text):
    domains = []
    for match in URL_RE.findall(text):
        m = match.lower()
        if m.startswith("@"):
            domains.append("t.me")  # упоминания трактуем как ссылку на telegram
            continue
        if not m.startswith("http"):
            m = "http://" + m
        try:
            host = urlparse(m).netloc
            if host.startswith("www."):
                host = host[4:]
            domains.append(host)
        except Exception:
            pass
    return domains

def _utf16_len(s: str) -> int:
    """Длина строки в UTF-16 code units — так Telegram считает offset/length."""
    return len(s.encode("utf-16-le")) // 2


def build_datetime_entity(prefix: str, placeholder: str, unix_ts: int,
                          fmt: str = "dd.MM.yyyy HH:mm"):
    """
    Возвращает MessageEntity типа date_time для подстроки `placeholder`,
    которая идёт сразу после `prefix` в тексте.
    Telegram покажет время в часовом поясе каждого пользователя.
    """
    from aiogram.types import MessageEntity
    return MessageEntity(
        type="date_time",
        offset=_utf16_len(prefix),
        length=_utf16_len(placeholder),
        unix_time=unix_ts,
        date_time_format=fmt,
    )

# Приветствие новых участников в группе
@router.message(F.new_chat_members)
async def on_new_member(message: Message):
    chat_id = message.chat.id
    if await db.get_setting(chat_id, "group_welcome_enabled") != "1":
        return
    text_tmpl = await db.get_setting(chat_id, "group_welcome_text")
    ttl = int(await db.get_setting(chat_id, "group_welcome_ttl"))
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        await db.save_member(chat_id, member.id, member.full_name, member.username or "")
        text = text_tmpl.replace("{name}", member.full_name)
        try:
            sent = await message.answer(text)
            if ttl > 0:
                asyncio.create_task(delete_later(message.bot, chat_id, sent.message_id, ttl))
        except Exception:
            pass


async def delete_later(bot, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# Модерация сообщений в группах
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate(message: Message):
    chat_id = message.chat.id
    if not message.from_user:
        return
    # Админов не трогаем
    if await is_user_admin(message.bot, chat_id, message.from_user.id):
        return

    text = message.text or message.caption or ""

    # Фильтр запрещённых слов
    if await db.get_setting(chat_id, "word_filter") == "1":
        banned = [w for _, w in await db.get_banned_words(chat_id)]
        low = text.lower()
        if any(w in low for w in banned):
            try:
                await message.delete()
            except Exception:
                pass
            return

    # Удаление ссылок (кроме белого списка доменов)
    if await db.get_setting(chat_id, "del_links") == "1":
        found = extract_domains(text)
        if found:
            allowed = [d for _, d in await db.get_allowed_domains(chat_id)]
            # ссылка разрешена, если её домен совпадает с разрешённым (или его поддомен)
            def is_allowed(host):
                return any(host == a or host.endswith("." + a) for a in allowed)
            if not all(is_allowed(h) for h in found):
                try:
                    await message.delete()
                except Exception:
                    pass
                return


# ============================================================
#                   МЕНЮ ЧАТА (общее)
# ============================================================
@router.callback_query(F.data.startswith("ch:"))
async def cb_chat(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else "чат"
    icon = "📢" if chat and chat[2] == "channel" else "👥"
    await c.message.edit_text(f"{icon} <b>{title}</b>\nЧто настроим?",
                              reply_markup=await kb.chat_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    now = int(time.time())
    total = await db.count_members(chat_id)
    today = await db.count_members(chat_id, since=now - 86400)
    last7 = await db.count_members(chat_id, since=now - 7 * 86400)
    status = "включён 🟢" if await db.is_auto_approve(chat_id) else "выключен 🔴"
    await c.message.edit_text(
        f"📊 <b>Статистика</b>\n\nВсего: <b>{total}</b>\nЗа сутки: <b>{today}</b>\n"
        f"За неделю: <b>{last7}</b>\nАвтоприём: {status}",
        reply_markup=kb.back_kb(f"ch:{chat_id}")
    )
    await c.answer()


@router.callback_query(F.data.startswith("last:"))
async def cb_last(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    rows = await db.get_last_members(chat_id, 10)
    if not rows:
        text = "Пока никого нет."
    else:
        lines = ["🕓 <b>Последние:</b>\n"]
        for fn, un, ja in rows:
            t = datetime.fromtimestamp(ja, TZ).strftime("%d.%m %H:%M")
            lines.append(f"• {fn} (@{un or '—'}) — {t}")
        text = "\n".join(lines)
    await c.message.edit_text(text, reply_markup=kb.back_kb(f"ch:{chat_id}"))
    await c.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    new_state = not await db.is_auto_approve(chat_id)
    await db.set_setting(chat_id, "auto_approve", "1" if new_state else "0")
    await c.message.edit_reply_markup(reply_markup=await kb.chat_menu_kb(chat_id))
    await c.answer("Автоприём " + ("включён" if new_state else "выключен"))


@router.callback_query(F.data.startswith("reset:"))
async def cb_reset(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await db.reset_settings(chat_id)
    await c.message.edit_reply_markup(reply_markup=await kb.chat_menu_kb(chat_id))
    await c.answer("Настройки сброшены")


# ============================================================
#            ПРИВЕТСТВИЕ В ЛИЧКУ (каналы) + КНОПКИ
# ============================================================
@router.callback_query(F.data.startswith("wmenu:"))
async def cb_wmenu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text("⚙️ <b>Приветствие (в личку)</b>",
                              reply_markup=await kb.welcome_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("wtoggle:"))
async def cb_wtoggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "welcome_enabled")
    await db.set_setting(chat_id, "welcome_enabled", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.welcome_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("st:"))
async def cb_st(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.welcome_text)
    await c.message.edit_text("✏️ Пришли текст приветствия.",
                              reply_markup=kb.back_kb(f"wmenu:{chat_id}"))
    await c.answer()


@router.message(Form.welcome_text)
async def in_welcome_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "welcome_text", message.html_text)
    await state.clear()
    await message.answer("✅ Текст обновлён.", reply_markup=await kb.welcome_menu_kb(cid))


@router.callback_query(F.data.startswith("sd:"))
async def cb_sd(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.welcome_delay)
    await c.message.edit_text("⏱ Задержка в секундах (0 — сразу).",
                              reply_markup=kb.back_kb(f"wmenu:{chat_id}"))
    await c.answer()


@router.message(Form.welcome_delay)
async def in_welcome_delay(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit():
        await message.answer("Нужно число.")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "welcome_delay", message.text.strip())
    await state.clear()
    await message.answer("✅ Задержка обновлена.", reply_markup=await kb.welcome_menu_kb(cid))


@router.callback_query(F.data.startswith("wbtns:"))
async def cb_wbtns(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🔘 <b>Кнопки приветствия</b> (до 4).\nНажми на кнопку, чтобы удалить.",
        reply_markup=await kb.welcome_buttons_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("addwbtn:"))
async def cb_addwbtn(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.wbtn_text)
    await c.message.edit_text("Пришли текст кнопки (что будет на ней написано).",
                              reply_markup=kb.back_kb(f"wbtns:{chat_id}"))
    await c.answer()


@router.message(Form.wbtn_text)
async def in_wbtn_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    await state.update_data(wbtn_text=message.text.strip())
    await state.set_state(Form.wbtn_url)
    await message.answer("Теперь пришли ссылку (http:// или https://).")


@router.message(Form.wbtn_url)
async def in_wbtn_url(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    url = message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    ok = await db.add_welcome_button(cid, data["wbtn_text"], url)
    await state.clear()
    msg = "✅ Кнопка добавлена." if ok else "⚠️ Уже 4 кнопки — больше нельзя."
    await message.answer(msg, reply_markup=await kb.welcome_buttons_kb(cid))


@router.callback_query(F.data.startswith("delwbtn:"))
async def cb_delwbtn(c: CallbackQuery):
    _, cid, bid = c.data.split(":")
    await db.delete_welcome_button(int(bid), int(cid))
    await c.message.edit_reply_markup(reply_markup=await kb.welcome_buttons_kb(int(cid)))
    await c.answer("Удалено")


@router.callback_query(F.data.startswith("show:"))
async def cb_show(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    text = await db.get_setting(chat_id, "welcome_text")
    buttons = await db.get_welcome_buttons(chat_id)
    keyboard = None
    if buttons:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, url=u)] for _, t, u in buttons
        ])
    await c.message.answer(text, reply_markup=keyboard)
    await c.answer("Так выглядит приветствие")


# ============================================================
#                   МОДЕРАЦИЯ (меню)
# ============================================================
@router.callback_query(F.data.startswith("mod:"))
async def cb_mod(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text("🛡 <b>Модерация группы</b>",
                              reply_markup=await kb.mod_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("tdl:"))
async def cb_tdl(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "del_links")
    await db.set_setting(chat_id, "del_links", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.mod_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("twf:"))
async def cb_twf(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "word_filter")
    await db.set_setting(chat_id, "word_filter", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.mod_menu_kb(chat_id))
    await c.answer()

@router.callback_query(F.data.startswith("treact:"))
async def cb_treact(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "auto_reaction")
    await db.set_setting(chat_id, "auto_reaction", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.chat_menu_kb(chat_id))
    await c.answer("Авто-реакция " + ("включена" if cur != "1" else "выключена"))

@router.callback_query(F.data.startswith("domains:"))
async def cb_domains(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🌐 <b>Разрешённые домены</b>\nСсылки на эти домены НЕ удаляются.\n"
        "Пример: <code>t.me</code>, <code>youtube.com</code>",
        reply_markup=await kb.domains_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("adddom:"))
async def cb_adddom(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.domain)
    await c.message.edit_text("Пришли домен без http (например: youtube.com).",
                              reply_markup=kb.back_kb(f"domains:{chat_id}"))
    await c.answer()


@router.message(Form.domain)
async def in_domain(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    domain = message.text.strip().lower().replace("http://", "").replace("https://", "").strip("/")
    data = await state.get_data()
    cid = data["chat_id"]
    await db.add_allowed_domain(cid, domain)
    await state.clear()
    await message.answer("✅ Домен добавлен.", reply_markup=await kb.domains_kb(cid))


@router.callback_query(F.data.startswith("deldom:"))
async def cb_deldom(c: CallbackQuery):
    _, cid, did = c.data.split(":")
    await db.delete_allowed_domain(int(did), int(cid))
    await c.message.edit_reply_markup(reply_markup=await kb.domains_kb(int(cid)))
    await c.answer("Удалено")


@router.callback_query(F.data.startswith("words:"))
async def cb_words(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🚫 <b>Запрещённые слова</b>\nСообщения с ними удаляются.",
        reply_markup=await kb.words_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("addword:"))
async def cb_addword(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.word)
    await c.message.edit_text("Пришли слово для блокировки.",
                              reply_markup=kb.back_kb(f"words:{chat_id}"))
    await c.answer()


@router.message(Form.word)
async def in_word(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.add_banned_word(cid, message.text.strip())
    await state.clear()
    await message.answer("✅ Слово добавлено.", reply_markup=await kb.words_kb(cid))


@router.callback_query(F.data.startswith("delword:"))
async def cb_delword(c: CallbackQuery):
    _, cid, wid = c.data.split(":")
    await db.delete_banned_word(int(wid), int(cid))
    await c.message.edit_reply_markup(reply_markup=await kb.words_kb(int(cid)))
    await c.answer("Удалено")


# ============================================================
#               ПРИВЕТСТВИЕ В ГРУППЕ
# ============================================================
@router.callback_query(F.data.startswith("gw:"))
async def cb_gw(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    ttl = await db.get_setting(chat_id, "group_welcome_ttl")
    txt = await db.get_setting(chat_id, "group_welcome_text")
    await c.message.edit_text(
        f"👋 <b>Приветствие в группе</b>\n"
        f"Автоудаление: {ttl} сек.\n"
        f"Можно использовать <code>{{name}}</code> — подставится имя.\n\n"
        f"Текущий текст:\n{txt}",
        reply_markup=await kb.group_welcome_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("gwtoggle:"))
async def cb_gwtoggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "group_welcome_enabled")
    await db.set_setting(chat_id, "group_welcome_enabled", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.group_welcome_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("gwtext:"))
async def cb_gwtext(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.gw_text)
    await c.message.edit_text(
        "✏️ Пришли текст приветствия для группы.\n"
        "<code>{name}</code> заменится на имя нового участника.",
        reply_markup=kb.back_kb(f"gw:{chat_id}")
    )
    await c.answer()


@router.message(Form.gw_text)
async def in_gw_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "group_welcome_text", message.html_text)
    await state.clear()
    await message.answer("✅ Текст обновлён.", reply_markup=await kb.group_welcome_kb(cid))


@router.callback_query(F.data.startswith("gwttl:"))
async def cb_gwttl(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.gw_ttl)
    await c.message.edit_text(
        "⏱ Через сколько секунд удалять приветствие? (0 — не удалять)",
        reply_markup=kb.back_kb(f"gw:{chat_id}")
    )
    await c.answer()


@router.message(Form.gw_ttl)
async def in_gw_ttl(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit():
        await message.answer("Нужно число.")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "group_welcome_ttl", message.text.strip())
    await state.clear()
    await message.answer("✅ Обновлено.", reply_markup=await kb.group_welcome_kb(cid))


# ============================================================
#               РАССЫЛКА И ПОСТЫ (каналы)
# ============================================================
@router.callback_query(F.data.startswith("bc:"))
async def cb_bc(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.broadcast)
    await c.message.edit_text("📨 Пришли сообщение для рассылки.",
                              reply_markup=kb.back_kb(f"ch:{chat_id}"))
    await c.answer()


@router.message(Form.broadcast)
async def in_broadcast(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await state.clear()
    text = message.html_text
    ids = await db.get_member_ids(cid)
    if not ids:
        await message.answer("Нет получателей.", reply_markup=await kb.chat_menu_kb(cid))
        return
    await message.answer(f"Рассылка по {len(ids)} получателям...")
    sent, failed = 0, 0
    for uid in ids:
        try:
            await message.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_PAUSE)
    await message.answer(f"📨 Готово. Доставлено: {sent}, нет: {failed}",
                         reply_markup=await kb.chat_menu_kb(cid))


@router.callback_query(F.data.startswith("posts:"))
async def cb_posts(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text("📝 <b>Отложенные посты</b>\nНажми, чтобы отменить.",
                              reply_markup=await kb.posts_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("newpost:"))
async def cb_newpost(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.post_text)
    await c.message.edit_text("📝 Шаг 1/3. Пришли текст поста.",
                              reply_markup=kb.back_kb(f"posts:{chat_id}"))
    await c.answer()


@router.message(Form.post_text)
async def in_post_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    await state.update_data(post_text=message.html_text)
    data = await state.get_data()
    await state.set_state(Form.post_button)
    await message.answer("🔘 Шаг 2/3. Кнопка в формате: Название | https://ссылка\nИли «Без кнопки».",
                         reply_markup=kb.skip_kb(data["chat_id"]))


@router.callback_query(F.data.startswith("nobtn:"))
async def cb_nobtn(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(btn_text=None, btn_url=None)
    await state.set_state(Form.post_time)
    await c.message.edit_text(
        f"🕓 Шаг 3/3. Дата и время: ДД.ММ.ГГГГ ЧЧ:ММ\n"
        f"Например: {datetime.now(TZ).strftime('%d.%m.%Y')} 20:00 (UTC+{TIMEZONE_OFFSET})",
        reply_markup=kb.back_kb(f"posts:{chat_id}")
    )
    await c.answer()


@router.message(Form.post_button)
async def in_post_button(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if "|" not in message.text:
        await message.answer("Формат: Название | https://ссылка")
        return
    bt, bu = [p.strip() for p in message.text.split("|", 1)]
    if not (bu.startswith("http://") or bu.startswith("https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    await state.update_data(btn_text=bt, btn_url=bu)
    await state.set_state(Form.post_time)
    await message.answer(
        f"🕓 Шаг 3/3. Дата и время: ДД.ММ.ГГГГ ЧЧ:ММ\n"
        f"Например: {datetime.now(TZ).strftime('%d.%m.%Y')} 20:00 (UTC+{TIMEZONE_OFFSET})"
    )


@router.message(Form.post_time)
async def in_post_time(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await message.answer("Формат: 25.12.2026 20:00")
        return
    pub = int(dt.timestamp())
    if pub <= int(time.time()):
        await message.answer("Это время уже прошло.")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.add_post(cid, data["post_text"], data.get("btn_text"), data.get("btn_url"), pub)
    await state.clear()
    await message.answer(f"✅ Запланировано на {dt.strftime('%d.%m.%Y %H:%M')}.",
                         reply_markup=await kb.posts_menu_kb(cid))


@router.callback_query(F.data.startswith("delpost:"))
async def cb_delpost(c: CallbackQuery):
    _, cid, pid = c.data.split(":")
    await db.cancel_post(int(pid), int(cid))
    await c.message.edit_reply_markup(reply_markup=await kb.posts_menu_kb(int(cid)))
    await c.answer("Отменено")
