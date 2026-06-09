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
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatType

import database as db
import keyboards as kb
from config import ADMIN_ID, TZ, TIMEZONE_OFFSET, BROADCAST_PAUSE

router = Router()
# Подхваченные кастомные реакции по постам: {(chat_id, message_id): custom_emoji_id}
_custom_reactions: dict[tuple[int, int], str] = {}
# Каналы, по которым прямо сейчас идёт перебор /reactall (защита от повторного запуска)
_reactall_running: set[int] = set()

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
    log_chat = State()


def is_admin_id(uid):
    return uid == ADMIN_ID

async def send_chat_log(bot, chat_id, text, reply_markup=None):

    """
    Шлёт уведомление по чату chat_id в его тему лог-группы.
    Если отправка в тему упала (тема удалена и т.п.) — пробует пересоздать
    тему и повторить. Если и это не вышло — fallback в личку админу.
    """
    log_chat = await db.get_global_log_chat()
    thread_id = await db.get_log_thread(chat_id)

    if await db.get_setting(chat_id, "log_disabled") == "1":
        return  # логи отключены — ничего не шлём
    if log_chat and thread_id:
        try:
            await bot.send_message(
                log_chat, text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            # тема, возможно, удалена — пробуем восстановить
            chat = await db.get_chat(chat_id)
            title = chat[1] if chat else str(chat_id)
            new_thread = await ensure_log_topic(bot, chat_id, title, force=True)
            if new_thread:
                try:
                    await bot.send_message(
                        log_chat, text,
                        message_thread_id=new_thread,
                        reply_markup=reply_markup,
                    )
                    return
                except Exception:
                    pass  # пересоздание не помогло — уходим в fallback

    # fallback в личку
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=reply_markup)
    except Exception:
        pass

async def send_to_topic(bot, chat_id, event_key, text, reply_markup=None):
    """
    Отправляет уведомление в тему лог-группы, привязанную к event_key.
    Если маршрут не настроен — шлёт админу в личку (старое поведение).
    chat_id — id управляемого канала/группы, к которому относится событие.
    """
    log_chat = await db.get_log_chat(chat_id)
    thread_id = await db.get_topic_route(chat_id, event_key)
    if log_chat and thread_id:
        try:
            await bot.send_message(
                log_chat, text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass  # тема удалена / нет прав — падаем в fallback ниже
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=reply_markup)
    except Exception:
        pass

async def ensure_log_topic(bot, chat_id, title, force=False):
    if await db.get_setting(chat_id, "log_disabled") == "1":
        return None  # логи для этого чата отключены вручную
    # уже привязана и не требуем пересоздания — ничего не делаем
    if not force:
        existing = await db.get_log_thread(chat_id)
        if existing:
            return existing

    log_chat = await db.get_global_log_chat()
    if not log_chat:
        return None  # лог-группа ещё не задана — пропускаем

    if chat_id == log_chat:
        return None  # это сама лог-группа, тему под неё не создаём

    if force:
        old_thread = await db.get_log_thread(chat_id)
        if old_thread:
            await db.delete_topics_by_thread(log_chat, old_thread)

    try:
        topic = await bot.create_forum_topic(chat_id=log_chat, name=title[:128])
    except Exception:
        return None  # нет прав / режим тем выключен

    thread_id = topic.message_thread_id
    # регистрируем тему в базе (чтобы была видна в /topics и в выборе)
    await db.add_topic(log_chat, thread_id, title[:128])
    # привязываем тему к чату
    await db.set_log_thread(chat_id, thread_id)

    # приветственное сообщение в новую тему
    try:
        await bot.send_message(
            log_chat,
            f"🧵 Тема для «{title}» создана. Сюда будет приходить вся информация по этому чату.",
            message_thread_id=thread_id,
        )
    except Exception:
        pass
    return thread_id

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
    if is_new:
        await ensure_log_topic(message.bot, chat.id, chat.title or str(chat.id))
    note = "\n\n⚠️ Автоприём выключен по умолчанию — включи его в меню." if is_new else ""
    await message.answer(f"✅ Канал «{chat.title}» добавлен. Открой /menu.{note}")


# Регистрация группы при добавлении бота
@router.my_chat_member()
async def on_added_to_chat(event: ChatMemberUpdated):
    chat = event.chat
    new_status = event.new_chat_member.status
    if new_status not in ("administrator", "member"):
        return

    if chat.type == "channel":
        is_new = await db.register_chat(chat.id, chat.title or str(chat.id), "channel")
        if is_new:
            await ensure_log_topic(event.bot, chat.id, chat.title or str(chat.id))
        if is_new:
            try:
                await event.bot.send_message(
                    ADMIN_ID,
                    f"✅ Бот добавлен в канал «{chat.title}».\n"
                    f"Открой /menu для настройки."
                )
            except Exception:
                pass
    elif chat.type in ("group", "supergroup"):
        is_new = await db.register_chat(chat.id, chat.title or str(chat.id), "group")
        if is_new:
            await ensure_log_topic(event.bot, chat.id, chat.title or str(chat.id))
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
async def _put_reaction(bot, chat_id, message_id, emoji):
    """Ставит реакцию и помечает в базе. Возвращает True при успехе."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
        await db.mark_reacted(chat_id, message_id)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return False
    except Exception:
        # удалённое/сервисное/несуществующее сообщение или нет прав
        return False


@router.channel_post()
async def auto_react_channel_post(message: Message):
    chat_id = message.chat.id
    if await db.get_setting(chat_id, "auto_reaction") != "1":
        return
    # ставим реакцию не сразу, а через задержку
    asyncio.create_task(_delayed_react(message.bot, chat_id, message.message_id))

@router.message_reaction_count()
async def on_reaction_count(event):
    from aiogram.types import ReactionTypeCustomEmoji
    chat_id, message_id = event.chat.id, event.message_id
    custom_id = None
    for r in event.reactions:
        if isinstance(r.type, ReactionTypeCustomEmoji):
            custom_id = r.type.custom_emoji_id
            break
    if not custom_id:
        return
    _custom_reactions[(chat_id, message_id)] = custom_id

    # Если авто-реакции включены и бот уже отреагировал (обычной) — заменим на кастомную
    if await db.get_setting(chat_id, "auto_reaction") != "1":
        return
    if await db.is_reacted(chat_id, message_id):
        try:
            await event.bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id,
                reaction=[ReactionTypeCustomEmoji(custom_emoji_id=custom_id)],
            )
        except Exception:
            pass  # кастомная больше недоступна / нет прав — оставляем как было

async def _delayed_react(bot, chat_id, message_id):
    delay = int(await db.get_setting(chat_id, "reaction_delay") or "180")
    if delay > 0:
        await asyncio.sleep(delay)
    if await db.is_reacted(chat_id, message_id):
        return
    # пробуем подхватить кастомную реакцию, которую уже поставили подписчики
    custom_id = _custom_reactions.get((chat_id, message_id))
    if custom_id:
        try:
            from aiogram.types import ReactionTypeCustomEmoji
            await bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id,
                reaction=[ReactionTypeCustomEmoji(custom_emoji_id=custom_id)],
            )
            await db.mark_reacted(chat_id, message_id)
            return
        except Exception:
            pass  # не вышло — поставим обычную ниже
    emoji = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    await _put_reaction(bot, chat_id, message_id, emoji)

async def _reactall_worker(bot, chat_id, top_message_id, depth, emoji, notify_to):
    """Фоновый перебор: ставит реакции на последние `depth` постов вниз от top_message_id."""
    done = 0          # успешно поставлено реакций
    skipped = 0       # уже были отреагированы
    start = max(1, top_message_id - depth + 1)
    try:
        for mid in range(top_message_id, start - 1, -1):
            if await db.is_reacted(chat_id, mid):
                skipped += 1
                continue
            ok = await _put_reaction(bot, chat_id, mid, emoji)
            if ok:
                done += 1
            await asyncio.sleep(0.4)  # бережём лимиты Telegram
        try:
            await bot.send_message(
                notify_to,
                f"✅ Готово. Поставлено реакций: {done}, "
                f"уже были: {skipped}, диапазон: {start}–{top_message_id}."
            )
        except Exception:
            pass
    finally:
        _reactall_running.discard(chat_id)  # снимаем флаг в любом случае

async def _clearall_worker(bot, chat_id, notify_to):
    """Снимает все реакции бота в канале по списку из reacted_posts."""
    ids = await db.get_reacted_ids(chat_id)
    removed = 0
    try:
        for mid in ids:
            try:
                await bot.set_message_reaction(
                    chat_id=chat_id, message_id=mid, reaction=[]
                )
                await db.unmark_reacted(chat_id, mid)
                removed += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                # сообщение удалено/недоступно — просто убираем из учёта
                await db.unmark_reacted(chat_id, mid)
            await asyncio.sleep(0.4)
        try:
            await bot.send_message(notify_to, f"✅ Снято реакций: {removed}.")
        except Exception:
            pass
    finally:
        _reactall_running.discard(chat_id)

@router.message(Command("reactall"), F.chat.type == ChatType.PRIVATE)
async def cmd_reactall(message: Message):
    if not is_admin_id(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(
            "Использование: <code>/reactall &lt;chat_id&gt; [глубина]</code>\n"
            "Пример: <code>/reactall -1001234567890 500</code>\n\n"
            "chat_id канала можно увидеть в /menu (или это id из списка чатов).\n"
            "Глубина по умолчанию 500, максимум 2000."
        )
        return

    chat_id = int(parts[1])
    depth = 500
    if len(parts) >= 3 and parts[2].isdigit():
        depth = max(1, min(int(parts[2]), 2000))  # ограничиваем 1..2000

    # Бот должен знать "верхний" message_id канала. Берём последний, на который реагировали,
    # либо просим переслать свежий пост, если истории ещё нет.
    top = await db.last_reacted_id(chat_id)
    if not top:
        await message.answer(
            "Не знаю последний пост этого канала. Сначала опубликуй любой новый пост "
            "(бот его отметит), потом запусти /reactall ещё раз."
        )
        return

    if chat_id in _reactall_running:
        await message.answer("⚠️ По этому каналу перебор уже идёт. Дождись отчёта.")
        return

    emoji = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    _reactall_running.add(chat_id)  # ставим флаг до запуска задачи
    await message.answer(
        f"⏳ Запускаю перебор {depth} постов вниз от id {top}. "
        f"Это займёт примерно {int(depth * 0.4)} сек. Пришлю отчёт по завершении."
    )
    asyncio.create_task(
        _reactall_worker(message.bot, chat_id, top, depth, emoji, message.from_user.id)
    )

@router.callback_query(F.data.startswith("reactall:"))
async def cb_reactall(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    chat_id = int(c.data.split(":")[1])
    if chat_id in _reactall_running:
        await c.answer("По этому каналу перебор уже идёт.", show_alert=True)
        return
    top = await db.last_reacted_id(chat_id)
    if not top:
        await c.answer(
            "Сначала опубликуй любой новый пост, потом запусти снова.",
            show_alert=True
        )
        return
    depth = 500
    emoji = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    _reactall_running.add(chat_id)
    await c.message.answer(
        f"⏳ Запускаю перебор {depth} постов вниз от id {top}. "
        f"Это займёт примерно {int(depth * 0.4)} сек. Пришлю отчёт по завершении."
    )
    asyncio.create_task(
        _reactall_worker(c.bot, chat_id, top, depth, emoji, c.from_user.id)
    )
    await c.answer("Запущено")

@router.callback_query(F.data.startswith("clearall:"))
async def cb_clearall(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    chat_id = int(c.data.split(":")[1])
    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, снять все", callback_data=f"clearallok:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"reactions:{chat_id}")],
    ])
    await c.message.edit_text(
        "🧹 Снять все реакции бота в этом канале?\n\n"
        "Бот уберёт свои реакции со всех постов, на которые реагировал.",
        reply_markup=kb_confirm
    )
    await c.answer()


@router.callback_query(F.data.startswith("clearallok:"))
async def cb_clearall_ok(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    if chat_id in _reactall_running:
        await c.answer("По этому каналу уже идёт операция. Дождись отчёта.", show_alert=True)
        return
    _reactall_running.add(chat_id)
    await c.message.edit_text("⏳ Снимаю реакции. Пришлю отчёт по завершении.")
    asyncio.create_task(_clearall_worker(c.bot, chat_id, c.from_user.id))
    await c.answer("Запущено")

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
    text = (
        f"📥 Заявка в «{request.chat.title}» ({status})\n"
        f"{user.full_name} | @{user.username or '—'} | <code>{user.id}</code>"
    )
    await send_chat_log(bot, request.chat.id, text)


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



# ============================================================
#                   ТЕМЫ ФОРУМА (в супергруппах)
# ============================================================
@router.message(Command("newtopic"), F.chat.type == "supergroup")
async def cmd_newtopic(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    name = message.text.removeprefix("/newtopic").strip()
    if not name:
        await message.reply("Использование: /newtopic Название темы")
        return
    try:
        topic = await message.bot.create_forum_topic(chat_id=message.chat.id, name=name)
        await db.add_topic(message.chat.id, topic.message_thread_id, name)
        await message.bot.send_message(
            message.chat.id,
            f"Тема «{name}» создана 🎉",
            message_thread_id=topic.message_thread_id,
        )
    except Exception:
        await message.reply(
            "Не удалось создать тему. Проверь, что в группе включён режим тем "
            "и у бота есть право «Управление темами»."
        )


@router.message(Command("topics"), F.chat.type == "supergroup")
async def cmd_topics(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    rows = await db.get_topics(message.chat.id)
    if not rows:
        await message.reply("Бот ещё не создавал тем в этой группе.")
        return
    lines = ["<b>Темы, созданные ботом:</b>\n"]
    for _id, thread_id, name in rows:
        lines.append(f"• {name} (id {thread_id})")
    await message.reply("\n".join(lines))


@router.message(Command("deltopic"), F.chat.type == "supergroup")
async def cmd_deltopic(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    arg = message.text.removeprefix("/deltopic").strip()
    if not arg.isdigit():
        await message.reply("Использование: /deltopic <id темы> (id смотри в /topics)")
        return
    thread_id = int(arg)
    try:
        await message.bot.delete_forum_topic(
            chat_id=message.chat.id, message_thread_id=thread_id
        )
    except Exception:
        await message.reply("Не удалось удалить тему в Telegram (возможно, уже удалена).")
    # из базы убираем в любом случае
    for db_id, t_id, _name in await db.get_topics(message.chat.id):
        if t_id == thread_id:
            await db.delete_topic(db_id, message.chat.id)
    await message.reply("Тема удалена.")


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
    if await db.get_setting(chat_id, "clean_service") == "1":
        try:
            await message.delete()
        except Exception:
            pass
    if await db.get_setting(chat_id, "group_welcome_enabled") != "1":
        return
    text_tmpl = await db.get_setting(chat_id, "group_welcome_text")
    ttl = int(await db.get_setting(chat_id, "group_welcome_ttl"))
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        await db.save_member(chat_id, member.id, member.full_name, member.username or "")
        await send_chat_log(
            message.bot, chat_id,
            f"➕ Новый участник: {member.full_name} | "
            f"@{member.username or '—'} | <code>{member.id}</code>"
        )
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

# Очистка системных/сервисных сообщений в группах
@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.content_type.in_({
        "new_chat_members",
        "left_chat_member",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "pinned_message",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "message_auto_delete_timer_changed",
        "video_chat_scheduled",
        "video_chat_started",
        "video_chat_ended",
        "video_chat_participants_invited",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_closed",
        "forum_topic_reopened",
    }),
)
async def clean_service_messages(message: Message):
    chat_id = message.chat.id
    if await db.get_setting(chat_id, "clean_service") != "1":
        return
    try:
        await message.delete()
    except Exception:
        pass  # нет прав / сообщение уже удалено

# Модерация сообщений в группах
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate(message: Message):
    chat_id = message.chat.id

    # Пост привязанного канала, автоматически пересланный в обсуждение — не трогаем
    if message.is_automatic_forward:
        return
    # Сообщение от имени канала/чата (sender_chat), а не от пользователя — не трогаем
    if message.sender_chat:
        return

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
                await send_chat_log(
                    message.bot, chat_id,
                    f"🛡 Удалено сообщение от {message.from_user.full_name} "
                    f"(@{message.from_user.username or '—'})"
                )
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
                    await send_chat_log(
                message.bot, chat_id,
                f"🛡 Удалено сообщение от {message.from_user.full_name} "
                f"(@{message.from_user.username or '—'})"
            )
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
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else "чат"
    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"resetok:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"ch:{chat_id}")],
    ])
    await c.message.edit_text(
        f"♻️ Сбросить все настройки чата «{title}»?\n\n"
        f"Будут сброшены: автоприём, приветствия, модерация, реакции, "
        f"привязка темы и т.д. Действие необратимо.",
        reply_markup=kb_confirm
    )
    await c.answer()


@router.callback_query(F.data.startswith("resetok:"))
async def cb_reset_ok(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await db.reset_settings(chat_id)
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else "чат"
    icon = "📢" if chat and chat[2] == "channel" else "👥"
    await c.message.edit_text(
        f"✅ Настройки сброшены.\n\n{icon} <b>{title}</b>\nЧто настроим?",
        reply_markup=await kb.chat_menu_kb(chat_id)
    )
    await c.answer("Настройки сброшены")

# ============================================================
#            ТЕМА ЛОГ-ГРУППЫ ДЛЯ КАЖДОГО ЧАТА
# ============================================================
@router.callback_query(F.data.startswith("logtopic:"))
async def cb_logtopic(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🧵 <b>Тема для логов этого чата</b>\n\n"
        "Вся информация по этому каналу/группе будет уходить в выбранную тему "
        "лог-группы. Темы создаются командой /newtopic прямо в лог-группе.",
        reply_markup=await kb.log_topic_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("setlogchat:"))
async def cb_setlogchat(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(route_chat_id=chat_id)
    await state.set_state(Form.log_chat)
    await c.message.edit_text(
        "Пришли ID супергруппы-лога (с включёнными темами).\n"
        "Это id вида <code>-100...</code>\n\n"
        "Бот должен быть в ней админом с правом «Управление темами».\n"
        "Эта группа общая для всех твоих чатов — задаётся один раз.",
        reply_markup=kb.back_kb(f"logtopic:{chat_id}")
    )
    await c.answer()


@router.message(Form.log_chat)
async def in_log_chat(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    val = message.text.strip()
    if not val.lstrip("-").isdigit():
        await message.answer("Нужен числовой ID группы (например -1001234567890).")
        return
    data = await state.get_data()
    cid = data["route_chat_id"]
    await db.set_global_log_chat(int(val))
    await state.clear()
    await message.answer(
        "✅ Лог-группа сохранена (общая для всех чатов).",
        reply_markup=await kb.log_topic_kb(cid)
    )


@router.callback_query(F.data.startswith("picklogtopic:"))
async def cb_picklogtopic(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    log_chat = await db.get_global_log_chat()
    if not log_chat:
        await c.answer("Сначала задай лог-группу.", show_alert=True)
        return
    await c.message.edit_text(
        "Выбери тему для этого чата:",
        reply_markup=await kb.log_topic_choice_kb(chat_id, log_chat)
    )
    await c.answer()

@router.callback_query(F.data.startswith("setlogtopic:"))
async def cb_setlogtopic(c: CallbackQuery):
    _, chat_id, thread_id = c.data.split(":")
    await db.set_log_thread(int(chat_id), int(thread_id))
    await c.message.edit_text(
        "🧵 <b>Тема для логов этого чата</b>",
        reply_markup=await kb.log_topic_kb(int(chat_id))
    )
    await c.answer("Тема привязана")

@router.callback_query(F.data.startswith("autotopics:"))
async def cb_autotopics(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    back_chat_id = int(c.data.split(":")[1])
    log_chat = await db.get_global_log_chat()
    if not log_chat:
        await c.answer("Сначала задай лог-группу.", show_alert=True)
        return
    created = 0
    for cid, title, ctype in await db.get_chats():
        if cid == log_chat:
            continue
        if await db.get_log_thread(cid):
            continue  # уже есть тема
        thread_id = await ensure_log_topic(c.bot, cid, title)
        if thread_id:
            created += 1
        await asyncio.sleep(0.5)  # бережём лимиты Telegram
    await c.message.edit_text(
        f"✅ Готово. Создано новых тем: {created}.",
        reply_markup=await kb.log_topic_kb(back_chat_id)
    )
    await c.answer("Готово")

@router.callback_query(F.data.startswith("logtoggle:"))
async def cb_logtoggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "log_disabled")
    await db.set_setting(chat_id, "log_disabled", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.log_topic_kb(chat_id))
    await c.answer("Логи " + ("выключены" if cur != "1" else "включены"))

@router.callback_query(F.data.startswith("deltopicchat:"))
async def cb_deltopicchat(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"deltopicchatok:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"logtopic:{chat_id}")],
    ])
    await c.message.edit_text(
        "🗑 Удалить тему этого чата в лог-группе?\n\n"
        "Тема будет удалена в Telegram, а логи по этому чату отключены, "
        "чтобы тема не создалась заново.",
        reply_markup=kb_confirm
    )
    await c.answer()


@router.callback_query(F.data.startswith("deltopicchatok:"))
async def cb_deltopicchat_ok(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    log_chat = await db.get_global_log_chat()
    thread_id = await db.get_log_thread(chat_id)

    # отключаем логи, чтобы тема не пересоздалась самовосстановлением
    await db.set_setting(chat_id, "log_disabled", "1")

    if log_chat and thread_id:
        try:
            await c.bot.delete_forum_topic(chat_id=log_chat, message_thread_id=thread_id)
        except Exception:
            pass
        await db.delete_topics_by_thread(log_chat, thread_id)

    # убираем привязку темы у чата
    await db.set_setting(chat_id, "log_thread_id", "")

    await c.message.edit_text(
        "✅ Тема удалена, логи по этому чату отключены.\n"
        "Чтобы снова получать логи — включи их и привяжи/создай тему.",
        reply_markup=await kb.log_topic_kb(chat_id)
    )
    await c.answer("Удалено")

# ============================================================
#            МАРШРУТИЗАЦИЯ УВЕДОМЛЕНИЙ ПО ТЕМАМ
# ============================================================
@router.callback_query(F.data.startswith("routes:"))
async def cb_routes(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🧵 <b>Темы для уведомлений</b>\n\n"
        "Выбери, в какую тему лог-группы слать каждый тип событий.\n"
        "Темы создаются командой /newtopic прямо в лог-группе.",
        reply_markup=await kb.topics_route_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("setloggrp:"))
async def cb_setloggrp(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(route_chat_id=chat_id)
    await state.set_state(Form.log_chat)
    await c.message.edit_text(
        "Пришли ID супергруппы-лога (с темами), куда слать уведомления.\n"
        "ID можно увидеть в /menu в списке чатов или это id вида -100...\n\n"
        "Бот должен быть в этой группе админом с правом «Управление темами».",
        reply_markup=kb.back_kb(f"routes:{chat_id}")
    )
    await c.answer()


@router.message(Form.log_chat)
async def in_log_chat(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    val = message.text.strip()
    if not val.lstrip("-").isdigit():
        await message.answer("Нужен числовой ID группы (например -1001234567890).")
        return
    data = await state.get_data()
    cid = data["route_chat_id"]
    await db.set_log_chat(cid, int(val))
    await state.clear()
    await message.answer(
        "✅ Лог-группа сохранена.",
        reply_markup=await kb.topics_route_kb(cid)
    )


@router.callback_query(F.data.startswith("pickroute:"))
async def cb_pickroute(c: CallbackQuery):
    _, chat_id, event_key = c.data.split(":")
    chat_id = int(chat_id)
    log_chat = await db.get_log_chat(chat_id)
    if not log_chat:
        await c.answer("Сначала выбери лог-группу.", show_alert=True)
        return
    await c.message.edit_text(
        "Выбери тему для этого типа уведомлений:",
        reply_markup=await kb.topic_choice_kb(chat_id, event_key, log_chat)
    )
    await c.answer()


@router.callback_query(F.data.startswith("setroute:"))
async def cb_setroute(c: CallbackQuery):
    _, chat_id, event_key, thread_id = c.data.split(":")
    await db.set_topic_route(int(chat_id), event_key, int(thread_id))
    await c.message.edit_text(
        "🧵 <b>Темы для уведомлений</b>",
        reply_markup=await kb.topics_route_kb(int(chat_id))
    )
    await c.answer("Тема привязана")

@router.callback_query(F.data.startswith("delchat:"))
async def cb_delchat(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else "чат"
    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"delchatok:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"ch:{chat_id}")],
    ])
    await c.message.edit_text(
        f"🗑 Удалить «{title}» из списка бота?\n\n"
        f"Бот перестанет управлять этим чатом. Сам чат в Telegram НЕ удаляется.",
        reply_markup=kb_confirm
    )
    await c.answer()


@router.callback_query(F.data.startswith("delchatok:"))
async def cb_delchat_ok(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await db.delete_chat(chat_id)
    await c.message.edit_text("✅ Удалено из списка.", reply_markup=await kb.chats_kb())
    await c.answer("Удалено")

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

@router.callback_query(F.data.startswith("tcs:"))
async def cb_tcs(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "clean_service")
    await db.set_setting(chat_id, "clean_service", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.mod_menu_kb(chat_id))
    await c.answer("Чистка системных " + ("включена" if cur != "1" else "выключена"))

@router.callback_query(F.data.startswith("reactions:"))
async def cb_reactions(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🔥 <b>Реакции на посты канала</b>",
        reply_markup=await kb.reactions_menu_kb(chat_id)
    )
    await c.answer()

@router.callback_query(F.data.startswith("treact:"))
async def cb_treact(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "auto_reaction")
    await db.set_setting(chat_id, "auto_reaction", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.reactions_menu_kb(chat_id))
    await c.answer("Авто-реакция " + ("включена" if cur != "1" else "выключена"))

@router.callback_query(F.data.startswith("pickreact:"))
async def cb_pickreact(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🎯 <b>Выбери реакцию</b>, которую бот будет ставить на посты:",
        reply_markup=await kb.reaction_pick_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("setreact:"))
async def cb_setreact(c: CallbackQuery):
    _, chat_id, emoji = c.data.split(":")
    await db.set_setting(int(chat_id), "reaction_emoji", emoji)
    await c.message.edit_reply_markup(reply_markup=await kb.reaction_pick_kb(int(chat_id)))
    await c.answer(f"Реакция: {emoji}")

@router.callback_query(F.data.startswith("pickdelay:"))
async def cb_pickdelay(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "⏱ <b>Через сколько ставить реакцию</b> после публикации поста?",
        reply_markup=await kb.reaction_delay_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("setdelay:"))
async def cb_setdelay(c: CallbackQuery):
    _, chat_id, secs = c.data.split(":")
    await db.set_setting(int(chat_id), "reaction_delay", secs)
    await c.message.edit_reply_markup(reply_markup=await kb.reaction_delay_kb(int(chat_id)))
    label = "сразу" if secs == "0" else f"{int(secs) // 60} мин" if int(secs) >= 60 else f"{secs} сек"
    await c.answer(f"Задержка: {label}")

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
    dead = []  # кто заблокировал бота — удалим из базы
    for uid in ids:
        try:
            await message.bot.send_message(uid, text)
            sent += 1
        except TelegramRetryAfter as e:
            # Telegram просит подождать — ждём и повторяем этого же получателя
            await asyncio.sleep(e.retry_after)
            try:
                await message.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            # пользователь заблокировал бота
            dead.append(uid)
            failed += 1
        except TelegramBadRequest:
            failed += 1
        await asyncio.sleep(BROADCAST_PAUSE)

    if dead:
        await db.remove_members(cid, dead)

    await message.answer(
        f"📨 Готово. Доставлено: {sent}, не доставлено: {failed}.\n"
        f"Удалено заблокировавших: {len(dead)}.",
        reply_markup=await kb.chat_menu_kb(cid)
    )


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
    await state.clear()
    prefix = "✅ Запланировано на "
    placeholder = "—"
    full_text = prefix + placeholder + "."
    try:
        entity = build_datetime_entity(prefix, placeholder, pub)
        await message.answer(full_text, entities=[entity],
                             reply_markup=await kb.posts_menu_kb(cid))
    except Exception:
        # Fallback, если версия aiogram/клиента не поддерживает date_time
        await message.answer(f"✅ Запланировано на {dt.strftime('%d.%m.%Y %H:%M')}.",
                             reply_markup=await kb.posts_menu_kb(cid))


@router.callback_query(F.data.startswith("delpost:"))
async def cb_delpost(c: CallbackQuery):
    _, cid, pid = c.data.split(":")
    await db.cancel_post(int(pid), int(cid))
    await c.message.edit_reply_markup(reply_markup=await kb.posts_menu_kb(int(cid)))
    await c.answer("Отменено")
