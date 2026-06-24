import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
import csv
import io
import json
from collections import defaultdict

from aiogram import Router, F
from aiogram.types import (
    ChatJoinRequest, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated,
    ReactionTypeEmoji, ChatPermissions, BufferedInputFile,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument
)
from aiogram.filters import Command
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatType, ContentType

import matplotlib
matplotlib.use("Agg")  # backend без GUI — обязательно до импорта pyplot
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import database as db
import keyboards as kb
from config import ADMIN_ID, TZ, TIMEZONE_OFFSET, BROADCAST_PAUSE

logger = logging.getLogger(__name__)
router = Router()
# Подхваченные кастомные реакции по постам: {(chat_id, message_id): custom_emoji_id}
_custom_reactions: dict[tuple[int, int], str] = {}
# Каналы, по которым прямо сейчас идёт перебор /reactall (защита от повторного запуска)
_reactall_running: set[int] = set()
# Кэш админов чатов: {chat_id: (set_of_admin_ids, timestamp)}
_admin_cache: dict[int, tuple[set[int], float]] = {}
_ADMIN_CACHE_TTL = 300  # секунд — обновляем список админов раз в 5 минут
# Ожидают прохождения капчи: {(chat_id, user_id): captcha_message_id}
_captcha_pending: dict[tuple[int, int], int] = {}
# Антифлуд: история времени сообщений {(chat_id, user_id): [timestamp, timestamp, ...]}
_flood_tracker: dict[tuple[int, int], list[float]] = {}
# Кого уже замутили за флуд — чтобы не мутить повторно каждым сообщением
_flood_muted: set[tuple[int, int]] = set()
# Буфер для сборки альбомов при создании поста: {media_group_id: {"items": [...], "task": Task, ...}}
_album_buffer: dict[str, dict] = {}

URL_RE = re.compile(r'(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+|@\w+)', re.IGNORECASE)


class Form(StatesGroup):
    welcome_text = State()
    welcome_delay = State()
    wbtn_text = State()
    wbtn_url = State()
    broadcast = State()
    post_text = State()
    post_media = State()
    post_button = State()
    post_poll = State()
    post_repeat = State()
    post_time = State()
    post_confirm = State()
    edit_post_text = State()
    edit_post_time = State()
    edit_post_button = State()
    edit_pub_text = State()
    gw_text = State()
    gw_ttl = State()
    domain = State()
    word = State()
    route_log_chat = State()
    captcha_time = State()
    warn_mute_limit = State()
    warn_ban_limit = State()
    warn_mute_minutes = State()
    flood_count = State()
    flood_window = State()
    flood_minutes = State()
    reactall_range = State()

def is_admin_id(uid):
    return uid == ADMIN_ID

# ============================================================
#            ПРОВЕРКА ПРАВ БОТА В ЧАТЕ + ДИАГНОСТИКА
# ============================================================
# Какие права нужны для каждой функции (атрибуты ChatMemberAdministrator).
_FEATURE_RIGHTS = {
    "Модерация (удаление, варны)": ["can_delete_messages", "can_restrict_members"],
    "Капча для новичков":          ["can_restrict_members"],
    "Антифлуд":                    ["can_restrict_members"],
    "Темы логов / форум":          ["can_manage_topics"],
    "Приём заявок":                ["can_invite_users"],
    "Публикация постов":           ["can_post_messages"],
}

# Подмножества для краткой сводки при добавлении (по типу чата)
_FEATURE_RIGHTS_CHANNEL = {
    "Публикация постов": ["can_post_messages"],
    "Приём заявок":      ["can_invite_users"],
}
_FEATURE_RIGHTS_GROUP = {
    "Модерация":  ["can_delete_messages", "can_restrict_members"],
    "Темы логов": ["can_manage_topics"],
    "Приём заявок": ["can_invite_users"],
}

# Человекочитаемые названия прав для подсказок
_RIGHT_LABELS = {
    "can_delete_messages": "Удаление сообщений",
    "can_restrict_members": "Блокировка участников",
    "can_invite_users": "Добавление участников / приём заявок",
    "can_manage_topics": "Управление темами",
    "can_post_messages": "Публикация сообщений",
    "can_pin_messages": "Закрепление",
}


async def collect_bot_rights(bot, chat_id):
    """
    Опрашивает get_chat_member(самого бота). Возвращает (status, rights_dict, error).
    status — 'administrator'/'member'/… либо None при ошибке.
    """
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as e:
        return None, {}, str(e)

    rights = {}
    for attr in (
        "can_delete_messages", "can_restrict_members", "can_invite_users",
        "can_manage_topics", "can_post_messages", "can_pin_messages",
        "can_promote_members", "can_change_info", "can_edit_messages",
    ):
        rights[attr] = bool(getattr(member, attr, False))
    return member.status, rights, None


def _format_rights_report(title, status, rights, error, chat_type):
    """Собирает читаемый отчёт с галочками по каждой функции."""
    if error:
        return (f"🔍 <b>Проверка прав: {title}</b>\n\n"
                f"❌ Не удалось получить данные: <code>{error}</code>\n\n"
                f"Убедись, что бот всё ещё состоит в чате.")

    lines = [f"🔍 <b>Проверка прав: {title}</b>\n"]
    if status != "administrator":
        lines.append("⚠️ Бот <b>не админ</b> — почти все функции работать не будут.\n")
    else:
        lines.append("✅ Бот является администратором.\n")

    if chat_type == "channel":
        irrelevant = {"Капча для новичков", "Антифлуд",
                      "Модерация (удаление, варны)", "Темы логов / форум"}
    else:
        irrelevant = {"Публикация постов"}

    for feature, needed in _FEATURE_RIGHTS.items():
        if feature in irrelevant:
            continue
        ok = all(rights.get(r, False) for r in needed)
        lines.append(f"{'✅' if ok else '❌'} {feature}")
        if not ok:
            missing = [r for r in needed if not rights.get(r, False)]
            human = ", ".join(_RIGHT_LABELS.get(m, m) for m in missing)
            lines.append(f"     └ не хватает: <i>{human}</i>")

    lines.append("\nНедостающие права выдаются в настройках чата Telegram "
                 "(управление администраторами → этот бот).")
    return "\n".join(lines)

async def safe_send(bot, chat_id, text, **kwargs):
    """
    Безопасная отправка сообщения: при флуд-лимите Telegram ждёт и повторяет один раз.
    Возвращает True при успехе, False при провале (заблокирован, нет прав и т.п.).
    """
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramRetryAfter as e:
        logger.info("Флуд-лимит, ждём %s сек перед повтором для %s", e.retry_after, chat_id)
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except Exception as e2:
            logger.warning("Повтор отправки в %s не удался: %s", chat_id, e2)
            return False
    except TelegramForbiddenError:
        return False  # пользователь заблокировал бота — это ожидаемо, не логируем
    except Exception as e:
        logger.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)
        return False

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
    except Exception as e:
        logger.error("Не удалось доставить лог даже в личку админу: %s", e)

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
    except Exception as e:
        logger.warning("Не удалось создать тему в лог-группе %s для чата %s: %s",
                       log_chat, chat_id, e)
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

@router.callback_query(F.data == "globalstatus")
async def cb_global_status(c: CallbackQuery, state: FSMContext):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    await state.clear()
    types = await db.count_chats_by_type()
    channels = types.get("channel", 0)
    groups = types.get("group", 0)
    pending = await db.count_pending_posts_all()
    with_topic = await db.count_chats_with_log_thread()
    log_chat = await db.get_global_log_chat()
    total = channels + groups
    log_line = f"задана (<code>{log_chat}</code>)" if log_chat else "не задана"
    await c.message.edit_text(
        "📋 <b>Сводка по всем чатам</b>\n\n"
        f"📢 Каналов: <b>{channels}</b>\n"
        f"👥 Групп: <b>{groups}</b>\n"
        f"📦 Всего чатов: <b>{total}</b>\n"
        "━━━━━━━━━━━━\n"
        f"⏳ Постов в очереди: <b>{pending}</b>\n"
        f"🧵 Лог-группа: {log_line}\n"
        f"🔗 Чатов с привязанной темой: <b>{with_topic}</b> из {total}",
        reply_markup=kb.global_status_kb()
    )
    await c.answer()


@router.callback_query(F.data == "rightsall")
async def cb_rights_all(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    await c.answer("Проверяю все чаты…")
    log_chat = await db.get_global_log_chat()
    problems = []
    checked = 0
    for cid, title, ctype in await db.get_chats():
        if cid == 0 or cid == log_chat:
            continue
        checked += 1
        status, rights, error = await collect_bot_rights(c.bot, cid)
        if error:
            problems.append(f"⚠️ «{title}»: ошибка ({error})")
            continue
        if status != "administrator":
            problems.append(f"❌ «{title}»: бот не админ")
            continue
        relevant = (_FEATURE_RIGHTS_CHANNEL if ctype == "channel"
                    else _FEATURE_RIGHTS_GROUP)
        missing = [feat for feat, needed in relevant.items()
                   if not all(rights.get(r, False) for r in needed)]
        if missing:
            problems.append(f"⚠️ «{title}»: нет прав для — {', '.join(missing)}")
        await asyncio.sleep(0.1)  # бережём лимиты

    if not problems:
        text = f"✅ Проверено чатов: {checked}. Везде всё в порядке."
    else:
        text = (f"🔍 Проверено чатов: {checked}. Найдены проблемы:\n\n"
                + "\n".join(problems))
    await c.message.edit_text(text, reply_markup=kb.back_kb("chats"))

@router.callback_query(F.data.startswith("rights:"))
async def cb_rights(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    chat_id = int(c.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else str(chat_id)
    ctype = chat[2] if chat else "group"
    await c.answer("Проверяю права…")
    status, rights, error = await collect_bot_rights(c.bot, chat_id)
    report = _format_rights_report(title, status, rights, error, ctype)
    await c.message.edit_text(report, reply_markup=kb.back_kb(f"ch:{chat_id}"))

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
# Регистрация группы при добавлении бота
@router.my_chat_member()
async def on_added_to_chat(event: ChatMemberUpdated):
    chat = event.chat
    new_status = event.new_chat_member.status
    if new_status not in ("administrator", "member"):
        return

    async def _rights_hint():
        status, rights, error = await collect_bot_rights(event.bot, chat.id)
        if error:
            return f"\n\n⚠️ Не удалось проверить права: {error}"
        if status != "administrator":
            return ("\n\n⚠️ Бот добавлен <b>не админом</b> — модерация, капча, "
                    "посты и приём заявок работать не будут. Выдай боту права админа.")
        relevant = (_FEATURE_RIGHTS_CHANNEL if chat.type == "channel"
                    else _FEATURE_RIGHTS_GROUP)
        missing = [feat for feat, needed in relevant.items()
                   if not all(rights.get(r, False) for r in needed)]
        if not missing:
            return "\n\n✅ Все нужные права на месте."
        return ("\n\n⚠️ Не хватает прав для: " + ", ".join(missing) +
                ".\nОткрой /menu → чат → «🔍 Проверить права» для деталей.")

    if chat.type == "channel":
        is_new = await db.register_chat(chat.id, chat.title or str(chat.id), "channel")
        if is_new:
            await ensure_log_topic(event.bot, chat.id, chat.title or str(chat.id))
            hint = await _rights_hint()
            try:
                await event.bot.send_message(
                    ADMIN_ID,
                    f"✅ Бот добавлен в канал «{chat.title}».\n"
                    f"Открой /menu для настройки.{hint}"
                )
            except Exception:
                pass
    elif chat.type in ("group", "supergroup"):
        is_new = await db.register_chat(chat.id, chat.title or str(chat.id), "group")
        if is_new:
            await ensure_log_topic(event.bot, chat.id, chat.title or str(chat.id))
            hint = await _rights_hint()
            try:
                await event.bot.send_message(
                    ADMIN_ID,
                    f"✅ Бот добавлен в группу «{chat.title}».\n"
                    f"Открой /menu для настройки.{hint}"
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
    except Exception as e:
        # удалённое/сервисное/несуществующее сообщение или нет прав
        logger.warning("Не удалось поставить реакцию в чат %s на сообщение %s: %s",
                       chat_id, message_id, e)
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

async def _reactall_range_worker(bot, chat_id, start_id, end_id, emoji, notify_to):
    """Ставит реакции на посты в диапазоне id [start_id..end_id].
    Если для поста уже есть подхваченная кастом-реакция в кэше — ставит её,
    иначе обычный эмодзи из набора."""
    done = 0       # успешно поставлено
    skipped = 0    # уже были отреагированы
    custom = 0     # из них кастом-эмодзи
    try:
        for mid in range(start_id, end_id + 1):
            if await db.is_reacted(chat_id, mid):
                skipped += 1
                continue
            # пробуем подхватить кастомку, если она была пойманной на свежем посте
            custom_id = _custom_reactions.get((chat_id, mid))
            ok = False
            if custom_id:
                try:
                    from aiogram.types import ReactionTypeCustomEmoji
                    await bot.set_message_reaction(
                        chat_id=chat_id, message_id=mid,
                        reaction=[ReactionTypeCustomEmoji(custom_emoji_id=custom_id)],
                    )
                    await db.mark_reacted(chat_id, mid)
                    ok = True
                    custom += 1
                except Exception:
                    ok = False  # кастомка недоступна — поставим обычную ниже
            if not ok:
                ok = await _put_reaction(bot, chat_id, mid, emoji)
            if ok:
                done += 1
            await asyncio.sleep(0.4)  # бережём лимиты Telegram
        try:
            await bot.send_message(
                notify_to,
                f"✅ Готово. Поставлено реакций: {done} "
                f"(из них кастом: {custom}), уже были: {skipped}, "
                f"диапазон: {start_id}–{end_id}."
            )
        except Exception:
            pass
    finally:
        _reactall_running.discard(chat_id)

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

@router.message(Command("backup"), F.chat.type == ChatType.PRIVATE)
async def cmd_backup(message: Message):
    if not is_admin_id(message.from_user.id):
        return
    import os
    path = f"backup_{int(time.time())}.db"
    try:
        await db.make_backup(path)
        with open(path, "rb") as f:
            data = f.read()
        await message.answer_document(
            BufferedInputFile(data, filename="bot_backup.db"),
            caption="💾 Резервная копия базы."
        )
    except Exception as e:
        logger.error("Бэкап не удался: %s", e)
        await message.answer(f"⚠️ Не удалось сделать бэкап: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

@router.message(Command("reactall"), F.chat.type == ChatType.PRIVATE)
async def cmd_reactall(message: Message):
    if not is_admin_id(message.from_user.id):
        return
    parts = message.text.split()
    # Формат: /reactall <chat_id> <from> <to>
    if len(parts) != 4 or not parts[1].lstrip("-").isdigit() \
            or not parts[2].isdigit() or not parts[3].isdigit():
        await message.answer(
            "Использование: <code>/reactall &lt;chat_id&gt; &lt;from&gt; &lt;to&gt;</code>\n"
            "Пример: <code>/reactall -1001234567890 100 250</code>\n\n"
            "За раз — не больше 500 постов. Удобнее пользоваться кнопкой "
            "«Реакции на старые посты» в меню канала."
        )
        return
    chat_id = int(parts[1])
    start_id, end_id = int(parts[2]), int(parts[3])
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    if start_id < 1:
        start_id = 1
    count = end_id - start_id + 1
    if count > 500:
        await message.answer(f"Диапазон слишком большой ({count}). Максимум 500.")
        return
    if chat_id in _reactall_running:
        await message.answer("⚠️ По этому каналу перебор уже идёт. Дождись отчёта.")
        return
    emoji = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    _reactall_running.add(chat_id)
    await message.answer(
        f"⏳ Запускаю перебор постов id {start_id}–{end_id} ({count} шт.). "
        f"Это займёт примерно {int(count * 0.4)} сек."
    )
    asyncio.create_task(
        _reactall_range_worker(message.bot, chat_id, start_id, end_id,
                               emoji, message.from_user.id)
    )

@router.callback_query(F.data.startswith("reactall:"))
async def cb_reactall(c: CallbackQuery, state: FSMContext):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    chat_id = int(c.data.split(":")[1])
    if chat_id in _reactall_running:
        await c.answer("По этому каналу перебор уже идёт.", show_alert=True)
        return
    await state.update_data(reactall_chat_id=chat_id)
    await state.set_state(Form.reactall_range)

    last_id = await db.last_reacted_id(chat_id)
    hint = (f"\n📌 Последний отмеченный пост: <b>{last_id}</b> "
            f"(можно отталкиваться от него).\n" if last_id else "")

    await c.message.edit_text(
        "🔁 <b>Реакции на старые посты</b>\n\n"
        "Пришлите диапазон id постов через пробел:\n"
        "<code>from to</code>\n"
        "Например: <code>100 250</code>\n"
        f"{hint}\n"
        "id поста виден в его ссылке: <code>t.me/канал/123</code> → id = 123.\n"
        "За раз — не больше 500 постов.\n\n"
        "<i>На старые посты ставятся только обычные эмодзи из набора. "
        "Кастом-эмодзи (премиум) здесь не подхватываются — это ограничение "
        "Telegram: бот не может узнать реакции старого поста. Подхват кастома "
        "работает только на новых постах в реальном времени.</i>",
        reply_markup=kb.back_kb(f"reactions:{chat_id}")
    )
    await c.answer()

@router.message(Form.reactall_range)
async def in_reactall_range(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    chat_id = data.get("reactall_chat_id")
    parts = (message.text or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await message.answer(
            "Нужно два числа через пробел, например: <code>100 250</code>"
        )
        return
    start_id, end_id = int(parts[0]), int(parts[1])
    if start_id > end_id:
        start_id, end_id = end_id, start_id  # переставим, если перепутали местами
    if start_id < 1:
        start_id = 1
    count = end_id - start_id + 1
    if count > 500:
        await message.answer(
            f"Диапазон слишком большой ({count} постов). За раз — не больше 500. "
            f"Сократи диапазон."
        )
        return

    if chat_id in _reactall_running:
        await message.answer("⚠️ По этому каналу перебор уже идёт. Дождись отчёта.")
        return

    await state.clear()
    emoji = await db.get_setting(chat_id, "reaction_emoji") or "🔥"
    _reactall_running.add(chat_id)
    await message.answer(
        f"⏳ Запускаю перебор постов id {start_id}–{end_id} ({count} шт.). "
        f"Это займёт примерно {int(count * 0.4)} сек. Пришлю отчёт по завершении.",
        reply_markup=await kb.reactions_menu_kb(chat_id)
    )
    asyncio.create_task(
        _reactall_range_worker(message.bot, chat_id, start_id, end_id,
                               emoji, message.from_user.id)
    )

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
    """Проверяет, админ ли пользователь. Список админов чата кэшируется на 5 минут."""
    cached = _admin_cache.get(chat_id)
    if cached and (time.time() - cached[1]) < _ADMIN_CACHE_TTL:
        return user_id in cached[0]
    # кэш протух или его нет — запрашиваем заново
    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = {a.user.id for a in admins}
        _admin_cache[chat_id] = (admin_ids, time.time())
        return user_id in admin_ids
    except Exception as e:
        logger.warning("Не удалось получить админов чата %s: %s", chat_id, e)
        # при ошибке не блокируем работу — считаем, что не админ
        return False

async def issue_warn(bot, chat_id, user, reason="нарушение"):
    """
    Выдаёт предупреждение пользователю и применяет наказание при достижении порога.
    user — объект пользователя (у него есть .id и .full_name).
    Возвращает текущее число предупреждений.
    """
    count = await db.add_warn(chat_id, user.id)
    mute_limit = int(await db.get_setting(chat_id, "warn_mute_limit"))
    ban_limit = int(await db.get_setting(chat_id, "warn_ban_limit"))
    mute_minutes = int(await db.get_setting(chat_id, "warn_mute_minutes"))

    note = ""
    # Бан имеет приоритет (порог выше)
    if count >= ban_limit:
        try:
            await bot.ban_chat_member(chat_id, user.id)
            note = f"\n🚫 Достигнут лимит ({ban_limit}) — пользователь забанен."
            await db.reset_warns(chat_id, user.id)
        except Exception as e:
            logger.warning("Не удалось забанить %s в %s: %s", user.id, chat_id, e)
    elif count >= mute_limit:
        try:
            until = datetime.now(TZ) + timedelta(minutes=mute_minutes)
            await bot.restrict_chat_member(
                chat_id, user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            note = f"\n🔇 Достигнут лимит ({mute_limit}) — мут на {mute_minutes} мин."
        except Exception as e:
            logger.warning("Не удалось замутить %s в %s: %s", user.id, chat_id, e)

    await send_chat_log(
        bot, chat_id,
        f"⚠️ Предупреждение для {user.full_name} "
        f"(<code>{user.id}</code>): {reason}\n"
        f"Всего предупреждений: {count}.{note}"
    )
    return count

async def check_flood(message: Message) -> bool:
    """
    Проверяет, флудит ли пользователь. Если да — мутит на короткий срок,
    удаляет сообщение и возвращает True (значит, сообщение уже обработано).
    """
    chat_id = message.chat.id
    if await db.get_setting(chat_id, "antiflood_enabled") != "1":
        return False
    user = message.from_user
    key = (chat_id, user.id)

    limit = int(await db.get_setting(chat_id, "antiflood_count"))
    window = int(await db.get_setting(chat_id, "antiflood_window"))
    now = time.time()

    # Берём историю и выкидываем всё, что старше окна
    history = _flood_tracker.get(key, [])
    history = [t for t in history if now - t < window]
    history.append(now)
    _flood_tracker[key] = history

    if len(history) < limit:
        return False  # флуда нет

    # Флуд обнаружен — мутим (если ещё не замутили)
    if key in _flood_muted:
        # уже замучен, просто чистим лишнее сообщение
        try:
            await message.delete()
        except Exception:
            pass
        return True

    _flood_muted.add(key)
    mute_minutes = int(await db.get_setting(chat_id, "antiflood_mute_minutes"))
    try:
        until = datetime.now(TZ) + timedelta(minutes=mute_minutes)
        await message.bot.restrict_chat_member(
            chat_id, user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    except Exception as e:
        logger.warning("Антифлуд: не удалось замутить %s в %s: %s", user.id, chat_id, e)
        _flood_muted.discard(key)  # не вышло — снимаем флаг, попробуем в следующий раз
        return True
    try:
        await message.delete()
    except Exception:
        pass

    # Сбрасываем историю и снимаем флаг мута через окно времени
    _flood_tracker.pop(key, None)
    asyncio.create_task(_clear_flood_flag(key, mute_minutes * 60))

    await send_chat_log(
        message.bot, chat_id,
        f"🌊 Антифлуд: {user.full_name} (<code>{user.id}</code>) "
        f"замучен на {mute_minutes} мин за флуд."
    )
    return True


async def _clear_flood_flag(key, delay):
    """Снимает флаг мута за флуд после истечения мута, чтобы при повторе снова сработало."""
    await asyncio.sleep(delay)
    _flood_muted.discard(key)


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
    except Exception as e:
        logger.warning("Ошибка создания темы в %s: %s", message.chat.id, e)
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

# ============================================================
#              КОМАНДЫ ПРЕДУПРЕЖДЕНИЙ (в группах)
# ============================================================
@router.message(Command("warn"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_warn(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответь этой командой на сообщение нарушителя.")
        return
    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply("Боту предупреждение не выдать.")
        return
    if await is_user_admin(message.bot, message.chat.id, target.id):
        await message.reply("Это админ — предупреждение не выдаётся.")
        return
    reason = message.text.removeprefix("/warn").strip() or "вручную админом"
    count = await issue_warn(message.bot, message.chat.id, target, reason)
    await message.reply(f"⚠️ {target.full_name}: предупреждение выдано. Всего: {count}.")


@router.message(Command("unwarn"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unwarn(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответь этой командой на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    count = await db.remove_warn(message.chat.id, target.id)
    await message.reply(f"➖ {target.full_name}: снято одно предупреждение. Осталось: {count}.")


@router.message(Command("warns"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_warns(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответь этой командой на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    count = await db.get_warns(message.chat.id, target.id)
    await message.reply(f"📋 {target.full_name}: предупреждений — {count}.")


@router.message(Command("resetwarns"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_resetwarns(message: Message):
    if not await is_user_admin(message.bot, message.chat.id, message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответь этой командой на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    await db.reset_warns(message.chat.id, target.id)
    await message.reply(f"🧹 {target.full_name}: все предупреждения сброшены.")

# ============================================================
#           НАСТРОЙКА ПРЕДУПРЕЖДЕНИЙ (меню)
# ============================================================
@router.callback_query(F.data.startswith("warns_menu:"))
async def cb_warns_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "⚠️ <b>Настройка предупреждений</b>\n\n"
        "Когда у пользователя накапливается заданное число предупреждений, "
        "бот автоматически мутит или банит его.\n\n"
        "Выдавать варны можно вручную (/warn ответом) или автоматически "
        "при срабатывании фильтров модерации.",
        reply_markup=await kb.warns_menu_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("wtogglemod:"))
async def cb_warn_toggle_mod(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "warn_on_moderation")
    await db.set_setting(chat_id, "warn_on_moderation", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.warns_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("wsetmute:"))
async def cb_warn_set_mute(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.warn_mute_limit)
    await c.message.edit_text(
        "🔇 После скольки предупреждений мутить? (например: 3)",
        reply_markup=kb.back_kb(f"warns_menu:{chat_id}")
    )
    await c.answer()


@router.message(Form.warn_mute_limit)
async def in_warn_mute_limit(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("Нужно целое число не меньше 1.")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "warn_mute_limit", message.text.strip())
    await state.clear()
    await message.answer("✅ Порог мута обновлён.", reply_markup=await kb.warns_menu_kb(cid))


@router.callback_query(F.data.startswith("wsetban:"))
async def cb_warn_set_ban(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.warn_ban_limit)
    await c.message.edit_text(
        "🚫 После скольки предупреждений банить? (например: 5)\n"
        "Должно быть больше порога мута.",
        reply_markup=kb.back_kb(f"warns_menu:{chat_id}")
    )
    await c.answer()


@router.message(Form.warn_ban_limit)
async def in_warn_ban_limit(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("Нужно целое число не меньше 1.")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    new_ban = int(message.text.strip())
    mute_limit = int(await db.get_setting(cid, "warn_mute_limit"))
    if new_ban <= mute_limit:
        await message.answer(
            f"Порог бана ({new_ban}) должен быть больше порога мута ({mute_limit}). "
            f"Сначала измени порог мута или введи большее число."
        )
        return
    await db.set_setting(cid, "warn_ban_limit", message.text.strip())
    await state.clear()
    await message.answer("✅ Порог бана обновлён.", reply_markup=await kb.warns_menu_kb(cid))


@router.callback_query(F.data.startswith("wsetminutes:"))
async def cb_warn_set_minutes(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.warn_mute_minutes)
    await c.message.edit_text(
        "⏱ На сколько минут мутить при достижении порога? (например: 60)",
        reply_markup=kb.back_kb(f"warns_menu:{chat_id}")
    )
    await c.answer()


@router.message(Form.warn_mute_minutes)
async def in_warn_mute_minutes(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("Нужно целое число не меньше 1 (минут).")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "warn_mute_minutes", message.text.strip())
    await state.clear()
    await message.answer("✅ Длительность мута обновлена.", reply_markup=await kb.warns_menu_kb(cid))

# ============================================================
#                   АНТИФЛУД (меню)
# ============================================================
@router.callback_query(F.data.startswith("flood_menu:"))
async def cb_flood_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🌊 <b>Антифлуд</b>\n\n"
        "Если пользователь отправляет слишком много сообщений за короткое время, "
        "бот мутит его на заданный срок и удаляет лишнее сообщение.\n\n"
        "⚠️ Боту нужно право ограничивать участников.",
        reply_markup=await kb.flood_menu_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("floodtoggle:"))
async def cb_flood_toggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "antiflood_enabled")
    await db.set_setting(chat_id, "antiflood_enabled", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.flood_menu_kb(chat_id))
    await c.answer("Антифлуд " + ("выключен" if cur == "1" else "включён"))


@router.callback_query(F.data.startswith("floodcount:"))
async def cb_flood_count(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.flood_count)
    await c.message.edit_text(
        "📊 Сколько сообщений считать флудом? (например: 5)",
        reply_markup=kb.back_kb(f"flood_menu:{chat_id}")
    )
    await c.answer()


@router.message(Form.flood_count)
async def in_flood_count(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 2:
        await message.answer("Нужно целое число не меньше 2.")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "antiflood_count", message.text.strip())
    await state.clear()
    await message.answer("✅ Лимит обновлён.", reply_markup=await kb.flood_menu_kb(cid))


@router.callback_query(F.data.startswith("floodwindow:"))
async def cb_flood_window(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.flood_window)
    await c.message.edit_text(
        "⏱ За сколько секунд считать сообщения? (например: 7)",
        reply_markup=kb.back_kb(f"flood_menu:{chat_id}")
    )
    await c.answer()


@router.message(Form.flood_window)
async def in_flood_window(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("Нужно целое число не меньше 1 (секунд).")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "antiflood_window", message.text.strip())
    await state.clear()
    await message.answer("✅ Окно обновлено.", reply_markup=await kb.flood_menu_kb(cid))


@router.callback_query(F.data.startswith("floodminutes:"))
async def cb_flood_minutes(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.flood_minutes)
    await c.message.edit_text(
        "🔇 На сколько минут мутить флудера? (например: 5)",
        reply_markup=kb.back_kb(f"flood_menu:{chat_id}")
    )
    await c.answer()


@router.message(Form.flood_minutes)
async def in_flood_minutes(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("Нужно целое число не меньше 1 (минут).")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "antiflood_mute_minutes", message.text.strip())
    await state.clear()
    await message.answer("✅ Длительность мута обновлена.", reply_markup=await kb.flood_menu_kb(cid))

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

    captcha_on = await db.get_setting(chat_id, "captcha_enabled") == "1"
    welcome_on = await db.get_setting(chat_id, "group_welcome_enabled") == "1"
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

        # Капча: ограничиваем и просим нажать кнопку
        if captcha_on:
            started = await _start_captcha(message, chat_id, member)
            if started:
                continue  # пока не пройдёт капчу — обычное приветствие не шлём

        # Обычное приветствие (если капча выключена или не сработала)
        if welcome_on:
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


async def _start_captcha(message: Message, chat_id, member):
    """
    Ограничивает новичку право писать и шлёт капчу с кнопкой.
    Возвращает True, если капча реально выставлена (бот смог ограничить).
    """
    bot = message.bot
    # 1) Забираем право писать
    try:
        await bot.restrict_chat_member(
            chat_id, member.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
    except Exception as e:
        # Бот не админ / нет права ограничивать — капчу не применяем
        logger.warning("Капча в %s: не удалось ограничить пользователя %s: %s",
                       chat_id, member.id, e)
        return False

    # 2) Шлём сообщение с кнопкой
    timeout = int(await db.get_setting(chat_id, "captcha_timeout"))
    kb_captcha = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я не бот", callback_data=f"cap:{chat_id}:{member.id}")]
    ])
    try:
        sent = await message.answer(
            f"👋 {member.full_name}, подтверди, что ты человек — нажми кнопку ниже "
            f"в течение {timeout} сек, иначе будешь удалён.",
            reply_markup=kb_captcha,
        )
    except Exception as e:
        logger.warning("Капча в %s: не удалось отправить сообщение: %s", chat_id, e)
        return False

    # 3) Запоминаем и запускаем таймер
    _captcha_pending[(chat_id, member.id)] = sent.message_id
    asyncio.create_task(_captcha_timeout(bot, chat_id, member.id, timeout))
    return True


async def _captcha_timeout(bot, chat_id, user_id, timeout):
    """Ждёт timeout секунд. Если капча не пройдена — наказывает по настройке."""
    await asyncio.sleep(timeout)
    cap_msg_id = _captcha_pending.pop((chat_id, user_id), None)
    if cap_msg_id is None:
        return  # уже прошёл капчу — ничего не делаем

    # Удаляем сообщение капчи
    try:
        await bot.delete_message(chat_id, cap_msg_id)
    except Exception:
        pass

    action = await db.get_setting(chat_id, "captcha_action")
    if action == "kick":
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)  # разбан, чтобы мог зайти заново
        except Exception as e:
            logger.warning("Капча в %s: не удалось кикнуть %s: %s", chat_id, user_id, e)
    # если action == "mute" — просто оставляем без права писать (он уже ограничен)

    await send_chat_log(
        bot, chat_id,
        f"🤖 Пользователь <code>{user_id}</code> не прошёл капчу "
        f"({'кикнут' if action == 'kick' else 'оставлен без права писать'})."
    )


@router.callback_query(F.data.startswith("cap:"))
async def cb_captcha_pass(c: CallbackQuery):
    """Нажатие кнопки 'Я не бот'. Реагировать может только сам новичок."""
    _, chat_id, user_id = c.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)

    # Кнопку должен нажать тот, для кого капча
    if c.from_user.id != user_id:
        await c.answer("Это не твоя капча 🙂", show_alert=True)
        return

    # Снимаем ограничение — возвращаем стандартные права
    try:
        await c.bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception as e:
        logger.warning("Капча в %s: не удалось вернуть права %s: %s", chat_id, user_id, e)

    # Убираем из ожидания и удаляем сообщение капчи
    _captcha_pending.pop((chat_id, user_id), None)
    try:
        await c.message.delete()
    except Exception:
        pass

    await c.answer("Добро пожаловать! ✅")

    # Шлём обычное приветствие группы, если оно включено
    if await db.get_setting(chat_id, "group_welcome_enabled") == "1":
        text_tmpl = await db.get_setting(chat_id, "group_welcome_text")
        ttl = int(await db.get_setting(chat_id, "group_welcome_ttl"))
        text = text_tmpl.replace("{name}", c.from_user.full_name)
        try:
            sent = await c.bot.send_message(chat_id, text)
            if ttl > 0:
                asyncio.create_task(delete_later(c.bot, chat_id, sent.message_id, ttl))
        except Exception:
            pass

# Очистка системных/сервисных сообщений в группах
@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.content_type.in_({
        ContentType.LEFT_CHAT_MEMBER,
        ContentType.NEW_CHAT_TITLE,
        ContentType.NEW_CHAT_PHOTO,
        ContentType.DELETE_CHAT_PHOTO,
        ContentType.PINNED_MESSAGE,
        ContentType.GROUP_CHAT_CREATED,
        ContentType.SUPERGROUP_CHAT_CREATED,
        ContentType.CHANNEL_CHAT_CREATED,
        ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED,
        ContentType.VIDEO_CHAT_SCHEDULED,
        ContentType.VIDEO_CHAT_STARTED,
        ContentType.VIDEO_CHAT_ENDED,
        ContentType.VIDEO_CHAT_PARTICIPANTS_INVITED,
        ContentType.FORUM_TOPIC_CREATED,
        ContentType.FORUM_TOPIC_EDITED,
        ContentType.FORUM_TOPIC_CLOSED,
        ContentType.FORUM_TOPIC_REOPENED,
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

    # Антифлуд (до остальных проверок)
    if await check_flood(message):
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
            if await db.get_setting(chat_id, "warn_on_moderation") == "1":
                await issue_warn(message.bot, chat_id, message.from_user,
                                 reason="запрещённое слово")
            else:
                await send_chat_log(
                    message.bot, chat_id,
                    f"🛡 Удалено сообщение от {message.from_user.full_name} "
                    f"(@{message.from_user.username or '—'})"
                )
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
                if await db.get_setting(chat_id, "warn_on_moderation") == "1":
                    await issue_warn(message.bot, chat_id, message.from_user,
                                     reason="запрещённая ссылка")
                else:
                    await send_chat_log(
                        message.bot, chat_id,
                        f"🛡 Удалено сообщение от {message.from_user.full_name} "
                        f"(@{message.from_user.username or '—'})"
                    )
                return


# ============================================================
#                       АНАЛИТИКА
# ============================================================
def _build_growth_chart(timestamps, title, tz):
    """
    Синхронно строит PNG-график накопительного прироста по датам вступления.
    Выполняется в отдельном потоке. Возвращает bytes (PNG) или None, если данных нет.
    """
    if not timestamps:
        return None

    # Группируем по дням (в нужном часовом поясе)
    per_day = defaultdict(int)
    for ts in timestamps:
        day = datetime.fromtimestamp(ts, tz).date()
        per_day[day] += 1

    days = sorted(per_day.keys())
    cumulative = []
    running = 0
    for d in days:
        running += per_day[d]
        cumulative.append(running)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(days, cumulative, marker="o", linewidth=2, color="#2481cc")
    ax.fill_between(days, cumulative, alpha=0.15, color="#2481cc")
    ax.set_title(title)
    ax.set_xlabel("Дата")
    ax.set_ylabel("Всего участников (накопительно)")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)  # обязательно закрываем фигуру, иначе утечка памяти
    buf.seek(0)
    return buf.getvalue()


@router.callback_query(F.data.startswith("chart:"))
async def cb_chart(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    chat_id = int(c.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else str(chat_id)

    timestamps = await db.get_join_timestamps(chat_id)
    if not timestamps:
        await c.answer("Пока нет данных для графика.", show_alert=True)
        return

    await c.answer("Строю график…")
    # Тяжёлую отрисовку выносим в поток, чтобы не блокировать бота
    png = await asyncio.to_thread(
        _build_growth_chart, timestamps, f"Прирост: {title}", TZ
    )
    if not png:
        await c.message.answer("Не удалось построить график.")
        return

    photo = BufferedInputFile(png, filename="growth.png")
    await c.message.answer_photo(
        photo,
        caption=f"📈 Прирост участников «{title}» (всего {len(timestamps)})."
    )


@router.callback_query(F.data.startswith("export:"))
async def cb_export(c: CallbackQuery):
    if not is_admin_id(c.from_user.id):
        await c.answer()
        return
    chat_id = int(c.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    title = chat[1] if chat else str(chat_id)

    rows = await db.get_all_members(chat_id)
    if not rows:
        await c.answer("Список участников пуст.", show_alert=True)
        return

    await c.answer("Готовлю файл…")

    # Формируем CSV в памяти
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "full_name", "username", "joined_at"])
    for user_id, full_name, username, joined_at in rows:
        when = datetime.fromtimestamp(joined_at, TZ).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([user_id, full_name or "", username or "", when])

    # CSV с BOM, чтобы Excel корректно открыл кириллицу
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    doc = BufferedInputFile(data, filename=f"members_{chat_id}.csv")
    await c.message.answer_document(
        doc,
        caption=f"📄 Участники «{title}» — {len(rows)} записей."
    )

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
    chat = await db.get_chat(chat_id)
    ctype = chat[2] if chat else "channel"
    markup = kb.poststats_kb(chat_id) if ctype == "channel" else kb.back_kb(f"ch:{chat_id}")
    await c.message.edit_text(
        f"📊 <b>Статистика</b>\n\nВсего: <b>{total}</b>\nЗа сутки: <b>{today}</b>\n"
        f"За неделю: <b>{last7}</b>\nАвтоприём: {status}",
        reply_markup=markup
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
    await state.set_state(Form.route_log_chat)
    await c.message.edit_text(
        "Пришли ID супергруппы-лога (с включёнными темами).\n"
        "Это id вида <code>-100...</code>\n\n"
        "Бот должен быть в ней админом с правом «Управление темами».\n"
        "Эта группа общая для всех твоих чатов — задаётся один раз.",
        reply_markup=kb.back_kb(f"logtopic:{chat_id}")
    )
    await c.answer()


@router.message(Form.route_log_chat)
async def in_route_log_chat(message: Message, state: FSMContext):
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
async def cb_reactions(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
#               КАПЧА ДЛЯ НОВИЧКОВ (меню)
# ============================================================
@router.callback_query(F.data.startswith("captcha:"))
async def cb_captcha_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "🤖 <b>Капча для новичков</b>\n\n"
        "Новый участник получает ограничение на запись и кнопку «Я не бот». "
        "Не нажал вовремя — наказывается по настройке.\n\n"
        "⚠️ Боту нужны права админа с возможностью ограничивать/банить участников.",
        reply_markup=await kb.captcha_menu_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("captoggle:"))
async def cb_captcha_toggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "captcha_enabled")
    await db.set_setting(chat_id, "captcha_enabled", "0" if cur == "1" else "1")
    await c.message.edit_reply_markup(reply_markup=await kb.captcha_menu_kb(chat_id))
    await c.answer("Капча " + ("выключена" if cur == "1" else "включена"))


@router.callback_query(F.data.startswith("capaction:"))
async def cb_captcha_action(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    cur = await db.get_setting(chat_id, "captcha_action")
    await db.set_setting(chat_id, "captcha_action", "mute" if cur == "kick" else "kick")
    await c.message.edit_reply_markup(reply_markup=await kb.captcha_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("captime:"))
async def cb_captcha_time(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.captcha_time)
    await c.message.edit_text(
        "⏱ Сколько секунд давать на прохождение капчи? (например: 120)",
        reply_markup=kb.back_kb(f"captcha:{chat_id}")
    )
    await c.answer()


@router.message(Form.captcha_time)
async def in_captcha_time(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit() or int(message.text.strip()) < 10:
        await message.answer("Нужно число не меньше 10 (секунд).")
        return
    data = await state.get_data()
    cid = data["chat_id"]
    await db.set_setting(cid, "captcha_timeout", message.text.strip())
    await state.clear()
    await message.answer("✅ Время обновлено.", reply_markup=await kb.captcha_menu_kb(cid))


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
        ok = await safe_send(message.bot, uid, text)
        if ok:
            sent += 1
        else:
            failed += 1
            dead.append(uid)  # не доставили — кандидат на удаление из базы
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
    await c.message.edit_text("📝 <b>Отложенные посты</b>\nНажми на пост, чтобы открыть.",
                              reply_markup=await kb.posts_menu_kb(chat_id))
    await c.answer()

@router.callback_query(F.data.startswith("poststats:"))
async def cb_poststats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    counts = await db.count_posts_by_status(chat_id)
    published = counts.get("published", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    pending = counts.get("pending", 0)
    total = published + failed + cancelled + pending
    await c.message.edit_text(
        f"📮 <b>Статистика постов</b>\n\n"
        f"✅ Опубликовано: <b>{published}</b>\n"
        f"⏳ В очереди: <b>{pending}</b>\n"
        f"❌ Провалено: <b>{failed}</b>\n"
        f"🚫 Отменено: <b>{cancelled}</b>\n"
        f"━━━━━━━━━━━━\n"
        f"📊 Всего: <b>{total}</b>",
        reply_markup=kb.back_kb(f"ch:{chat_id}")
    )
    await c.answer()

@router.callback_query(F.data.startswith("posthist:"))
async def cb_posthist(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "📜 <b>История постов</b>\nПоследние 20 завершённых. Нажми, чтобы посмотреть.",
        reply_markup=await kb.post_history_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("histcard:"))
async def cb_histcard(c: CallbackQuery):
    _, cid, pid = c.data.split(":")
    post = await db.get_post(int(pid))
    if not post or post[1] != int(cid):
        await c.answer("Запись не найдена (возможно, удалена при очистке).", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=await kb.post_history_kb(int(cid)))
        return
    # post = (id, chat_id, text, btn_text, btn_url, publish_at, media_type, media_id, repeat_mode)
    text = post[2]
    media_type = post[6]
    media_names = {"photo": "🖼 Фото", "video": "🎬 Видео",
                   "document": "📎 Документ", "album": "🗂 Альбом"}
    when = datetime.fromtimestamp(post[5], TZ).strftime("%d.%m.%Y %H:%M")
    body = text or "<i>(без текста)</i>"
    if len(body) > 1000:
        body = body[:1000] + "…"
    kb_back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К истории", callback_data=f"posthist:{cid}")]
    ])
    await c.message.edit_text(
        f"📄 <b>Пост от {when}</b>\n"
        f"📦 Тип: {media_names.get(media_type, '📝 Текст')}\n\n"
        f"<b>Текст:</b>\n{body}",
        reply_markup=kb_back,
        disable_web_page_preview=True,
    )
    await c.answer()


@router.callback_query(F.data.startswith("postclean:"))
async def cb_postclean(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    removed = await db.delete_finished_posts(chat_id)
    counts = await db.count_posts_by_status(chat_id)
    published = counts.get("published", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    pending = counts.get("pending", 0)
    total = published + failed + cancelled + pending
    await c.message.edit_text(
        f"🧹 Удалено завершённых записей: <b>{removed}</b>.\n\n"
        f"📮 <b>Статистика постов</b>\n\n"
        f"✅ Опубликовано: <b>{published}</b>\n"
        f"⏳ В очереди: <b>{pending}</b>\n"
        f"❌ Провалено: <b>{failed}</b>\n"
        f"🚫 Отменено: <b>{cancelled}</b>\n"
        f"━━━━━━━━━━━━\n"
        f"📊 Всего: <b>{total}</b>",
        reply_markup=kb.poststats_kb(chat_id)
    )
    await c.answer("Очищено")

@router.callback_query(F.data.startswith("newpost:"))
async def cb_newpost(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id, media_type=None, media_id=None)
    await state.set_state(Form.post_text)
    await c.message.edit_text(
        "📝 Шаг 1/5. Пришли текст поста, либо фото/видео/документ/альбом "
        "(можно с подписью).",
        reply_markup=kb.back_kb(f"posts:{chat_id}")
    )
    await c.answer()


@router.message(Form.post_text)
async def in_post_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return

    data = await state.get_data()
    cid = data["chat_id"]

    # --- АЛЬБОМ (несколько медиа с общим media_group_id) ---
    if message.media_group_id:
        mgid = message.media_group_id
        item = _extract_media_item(message)
        if item is None:
            return  # неподдерживаемая часть альбома — игнорируем

        buf = _album_buffer.get(mgid)
        if buf is None:
            buf = {"items": [], "task": None, "chat_id": cid,
                   "user_id": message.from_user.id, "state": state}
            _album_buffer[mgid] = buf
        buf["items"].append(item)

        # caption берём с первой части, где он есть
        if message.caption and not buf.get("caption"):
            buf["caption"] = message.html_text

        # перезапускаем таймер сборки — ждём 1.5 сек после последней части
        if buf["task"]:
            buf["task"].cancel()
        buf["task"] = asyncio.create_task(_finish_album(mgid, message))
        return

    # --- ОДИНОЧНОЕ МЕДИА ---
    item = _extract_media_item(message)
    if item is not None:
        await state.update_data(
            post_text=message.html_text if message.caption else "",
            media_type=item["type"],
            media_id=item["file_id"],
        )
        await _ask_post_button(message, cid, state)
        return

    # --- ПРОСТО ТЕКСТ ---
    if message.text:
        await state.update_data(
            post_text=message.html_text,
            media_type=None,
            media_id=None,
        )
        await _ask_post_button(message, cid, state)
        return

    await message.answer("Пришли текст поста, либо фото/видео/документ/альбом.")

def _poll_options(options):
    """Готовит список вариантов для send_poll: новые aiogram требуют InputPollOption,
    старые — список строк. Пробуем новый формат, при неудаче откатываемся на строки."""
    try:
        from aiogram.types import InputPollOption
        return [InputPollOption(text=o) for o in options]
    except Exception:
        return list(options)

async def _is_channel(chat_id):
    chat = await db.get_chat(chat_id)
    return bool(chat and chat[2] == "channel")

def _extract_media_item(message: Message):
    """Достаёт из сообщения тип медиа и file_id. Возвращает dict или None."""
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id}
    if message.document:
        return {"type": "document", "file_id": message.document.file_id}
    return None


async def _finish_album(mgid, message: Message):
    """Срабатывает через паузу после последней части альбома — собирает всё вместе."""
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        return  # пришла ещё одна часть — этот таймер отменён, ждём новый

    buf = _album_buffer.pop(mgid, None)
    if not buf:
        return
    state: FSMContext = buf["state"]
    cid = buf["chat_id"]

    # media_id для альбома — JSON-список объектов {type, file_id}
    await state.update_data(
        post_text=buf.get("caption", ""),
        media_type="album",
        media_id=json.dumps(buf["items"]),
    )
    await _ask_post_button(message, cid, state)


async def _ask_post_button(message: Message, cid, state: FSMContext):
    await state.set_state(Form.post_button)
    await message.answer(
        "🔘 Шаг 2/5. Кнопка в формате: Название | https://ссылка\n"
        "Или нажми «Без кнопки».",
        reply_markup=kb.skip_kb(cid)
    )

async def _ask_post_poll(message: Message, cid, state: FSMContext):
    await state.set_state(Form.post_poll)
    await message.answer(
        "📊 Шаг 3/5. Прикрепить опрос к посту?\n\n"
        "Пришли опрос в формате:\n"
        "<code>Вопрос ? Вариант1 ; Вариант2 ; Вариант3</code>\n\n"
        "Вопрос — до «?», варианты — через «;» (2–10 шт.).\n"
        "Или нажми «Без опроса».",
        reply_markup=kb.poll_skip_kb(cid)
    )

@router.callback_query(F.data.startswith("nobtn:"))
async def cb_nobtn(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(btn_text=None, btn_url=None)
    await _ask_post_poll(c.message, chat_id, state)
    await c.answer()


@router.message(Form.post_button)
async def in_post_button(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if "|" not in (message.text or ""):
        await message.answer("Формат: Название | https://ссылка")
        return
    bt, bu = [p.strip() for p in message.text.split("|", 1)]
    if not (bu.startswith("http://") or bu.startswith("https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    data = await state.get_data()
    await state.update_data(btn_text=bt, btn_url=bu)
    await _ask_post_poll(message, data["chat_id"], state)

@router.callback_query(F.data.startswith("nopoll:"))
async def cb_nopoll(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(poll_json=None)
    await state.set_state(Form.post_repeat)
    await c.message.edit_text(
        "🔁 Шаг 4/5. Как часто публиковать?",
        reply_markup=kb.repeat_kb(chat_id)
    )
    await c.answer()


@router.message(Form.post_poll)
async def in_post_poll(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if "?" not in raw:
        await message.answer("Формат: Вопрос ? Вариант1 ; Вариант2 ; …")
        return
    question, opts_part = raw.split("?", 1)
    question = question.strip()
    options = [o.strip() for o in opts_part.split(";") if o.strip()]
    if not question:
        await message.answer("Вопрос пустой. Формат: Вопрос ? Вариант1 ; Вариант2")
        return
    if not (2 <= len(options) <= 10):
        await message.answer("Нужно от 2 до 10 вариантов (через «;»).")
        return
    if len(question) > 300:
        await message.answer("Вопрос слишком длинный (макс. 300 символов).")
        return
    if any(len(o) > 100 for o in options):
        await message.answer("Каждый вариант — максимум 100 символов.")
        return
    cid = (await state.get_data())["chat_id"]
    is_channel = await _is_channel(cid)
    # Сохраняем опрос с настройками по умолчанию: анонимный, одиночный выбор.
    # Для каналов анонимность принудительная (публичные опросы там запрещены).
    poll_data = {
        "question": question,
        "options": options,
        "is_anonymous": True,
        "allows_multiple_answers": False,
    }
    await state.update_data(poll_json=json.dumps(poll_data, ensure_ascii=False))
    note = ("\n\nℹ️ В каналах опрос всегда анонимный (ограничение Telegram)."
            if is_channel else "")
    await message.answer(
        f"✅ Опрос принят: «{question}» ({len(options)} вар.){note}\n\n"
        "⚙️ Настрой опрос или нажми «Готово»:",
        reply_markup=kb.poll_options_kb(
            cid, is_anonymous=True, multiple=False, is_channel=is_channel
        )
    )

@router.callback_query(F.data.startswith("polltgl:"))
async def cb_poll_toggle(c: CallbackQuery, state: FSMContext):
    _, what, chat_id = c.data.split(":")
    chat_id = int(chat_id)
    data = await state.get_data()
    raw = data.get("poll_json")
    if not raw:
        await c.answer("Опрос не найден, начни заново.", show_alert=True)
        return
    poll = json.loads(raw)
    is_channel = await _is_channel(chat_id)
    if what == "anon":
        if is_channel:
            await c.answer("В каналах опрос может быть только анонимным.", show_alert=True)
            return
        poll["is_anonymous"] = not poll.get("is_anonymous", True)
    elif what == "multi":
        poll["allows_multiple_answers"] = not poll.get("allows_multiple_answers", False)
    await state.update_data(poll_json=json.dumps(poll, ensure_ascii=False))
    await c.message.edit_reply_markup(
        reply_markup=kb.poll_options_kb(
            chat_id,
            is_anonymous=poll.get("is_anonymous", True),
            multiple=poll.get("allows_multiple_answers", False),
            is_channel=is_channel,
        )
    )
    await c.answer()


@router.callback_query(F.data.startswith("polldone:"))
async def cb_poll_done(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.set_state(Form.post_repeat)
    await c.message.edit_text(
        "🔁 Шаг 4/5. Как часто публиковать?",
        reply_markup=kb.repeat_kb(chat_id)
    )
    await c.answer()

@router.callback_query(F.data.startswith("setrepeat:"), Form.post_repeat)
async def cb_set_repeat(c: CallbackQuery, state: FSMContext):
    _, chat_id, mode = c.data.split(":")
    await state.update_data(repeat_mode=mode)
    await state.set_state(Form.post_time)
    label = {"once": "разово", "daily": "каждый день", "weekly": "каждую неделю"}.get(mode, "разово")
    await c.message.edit_text(
        f"🕓 Шаг 5/5. Повтор: {label}.\n"
        f"Дата и время первой публикации: ДД.ММ.ГГГГ ЧЧ:ММ\n"
        f"Например: {datetime.now(TZ).strftime('%d.%m.%Y')} 20:00 (UTC+{TIMEZONE_OFFSET})",
        reply_markup=kb.back_kb(f"posts:{chat_id}")
    )
    await c.answer()

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
    # сохраняем время в состоянии — пост создадим только после подтверждения
    await state.update_data(publish_at=pub)

    # 1) показываем сам пост так, как он уйдёт в канал
    await message.answer("👁 <b>Так будет выглядеть пост:</b>")
    try:
        await _send_post(
            message.bot, message.chat.id,
            data.get("post_text") or "",
            data.get("btn_text"), data.get("btn_url"),
            data.get("media_type"), data.get("media_id"),
            data.get("poll_json"),
        )
    except Exception as e:
        logger.warning("Предпросмотр поста не удался: %s", e)
        await message.answer("⚠️ Не удалось показать предпросмотр медиа, но пост можно запланировать.")

    # 2) служебная карточка с подтверждением
    repeat_names = {"once": "разово", "daily": "каждый день", "weekly": "каждую неделю"}
    repeat_label = repeat_names.get(data.get("repeat_mode", "once"), "разово")
    await message.answer(
        f"🕓 Публикация: <b>{dt.strftime('%d.%m.%Y %H:%M')}</b> (UTC+{TIMEZONE_OFFSET})\n"
        f"🔁 Повтор: {repeat_label}\n\n"
        f"Всё верно?",
        reply_markup=kb.post_confirm_kb(cid)
    )
    await state.set_state(Form.post_confirm)

async def _edit_published(bot, chat_id, message_id, new_text, media_type, btn_text, btn_url):
    """Редактирует текст/подпись и кнопку опубликованного сообщения.
    media_type=None → текстовое сообщение (edit_message_text),
    иначе медиа (edit_message_caption). Возвращает True при успехе."""
    keyboard = None
    if btn_text and btn_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, url=btn_url)]
        ])
    try:
        if media_type in (None, "", "text"):
            await bot.edit_message_text(
                new_text or "(пустой пост)",
                chat_id=chat_id, message_id=message_id,
                reply_markup=keyboard,
            )
        else:
            # фото/видео/документ/альбом — правим подпись первого сообщения
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=new_text or None,
                reply_markup=keyboard if media_type != "album" else None,
            )
        return True
    except Exception as e:
        logger.warning("Не удалось отредактировать опубликованное сообщение %s в %s: %s",
                       message_id, chat_id, e)
        return False

async def _send_post(bot, chat_id, text, btn_text, btn_url, media_type, media_id,
                     poll_json=None):
    """Отправляет пост (текст/медиа/альбом) и, при наличии, опрос отдельным сообщением.
    Используется и для предпросмотра, и из меню."""
    keyboard = None
    if btn_text and btn_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, url=btn_url)]
        ])

    if media_type == "photo":
        await bot.send_photo(chat_id, media_id, caption=text or None, reply_markup=keyboard)
    elif media_type == "video":
        await bot.send_video(chat_id, media_id, caption=text or None, reply_markup=keyboard)
    elif media_type == "document":
        await bot.send_document(chat_id, media_id, caption=text or None, reply_markup=keyboard)
    elif media_type == "album":
        items = json.loads(media_id)
        media = []
        for i, it in enumerate(items):
            cap = (text or None) if i == 0 else None
            if it["type"] == "photo":
                media.append(InputMediaPhoto(media=it["file_id"], caption=cap))
            elif it["type"] == "video":
                media.append(InputMediaVideo(media=it["file_id"], caption=cap))
            elif it["type"] == "document":
                media.append(InputMediaDocument(media=it["file_id"], caption=cap))
        await bot.send_media_group(chat_id, media)
        if keyboard:
            await bot.send_message(chat_id, text or "⬆️", reply_markup=keyboard)
    elif text:
        await bot.send_message(chat_id, text or "(пустой пост)", reply_markup=keyboard)

    # Прикреплённый опрос — отдельным сообщением
    if poll_json:
        try:
            poll = json.loads(poll_json)
            anon = poll.get("is_anonymous", True)
            if await _is_channel(chat_id):
                anon = True  # в каналах только анонимный
            await bot.send_poll(
                chat_id,
                question=poll["question"],
                options=_poll_options(poll["options"]),
                is_anonymous=anon,
                allows_multiple_answers=poll.get("allows_multiple_answers", False),
            )
        except Exception as e:
            logger.warning("Не удалось отправить опрос (предпросмотр) в %s: %s", chat_id, e)

@router.callback_query(F.data.startswith("postsave:"), Form.post_confirm)
async def cb_post_save(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    data = await state.get_data()
    pub = data.get("publish_at")
    if not pub:
        await c.answer("Время потерялось, начни заново.", show_alert=True)
        await state.clear()
        return
    await db.add_post(
        chat_id,
        data.get("post_text") or "",
        data.get("btn_text"),
        data.get("btn_url"),
        pub,
        media_type=data.get("media_type"),
        media_id=data.get("media_id"),
        repeat_mode=data.get("repeat_mode", "once"),
        poll_json=data.get("poll_json"),
    )
    await state.clear()
    dt = datetime.fromtimestamp(pub, TZ)
    await c.message.edit_text(
        f"✅ Запланировано на {dt.strftime('%d.%m.%Y %H:%M')}.",
        reply_markup=await kb.posts_menu_kb(chat_id)
    )
    await c.answer("Запланировано")


@router.callback_query(F.data.startswith("postcancel:"), Form.post_confirm)
async def cb_post_cancel(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.clear()
    await c.message.edit_text(
        "❌ Создание поста отменено.\n\n📝 <b>Отложенные посты</b>\nНажми на пост, чтобы открыть.",
        reply_markup=await kb.posts_menu_kb(chat_id)
    )
    await c.answer("Отменено")

def _format_post_card(post):
    """Собирает текстовое описание поста для карточки.
    post = (id, chat_id, text, btn_text, btn_url, publish_at, media_type,
            media_id, repeat_mode, sent_message_id, poll_json)"""
    (_id, _cid, text, btn_text, btn_url, publish_at, media_type,
     media_id, repeat_mode, sent_message_id, poll_json) = post
    media_names = {"photo": "🖼 Фото", "video": "🎬 Видео",
                   "document": "📎 Документ", "album": "🗂 Альбом"}
    repeat_names = {"once": "разово", "daily": "каждый день", "weekly": "каждую неделю"}
    when = datetime.fromtimestamp(publish_at, TZ).strftime("%d.%m.%Y %H:%M")
    lines = ["📝 <b>Отложенный пост</b>\n"]
    lines.append(f"🕓 Публикация: <b>{when}</b> (UTC+{TIMEZONE_OFFSET})")
    lines.append(f"🔁 Повтор: {repeat_names.get(repeat_mode, 'разово')}")
    lines.append(f"📦 Тип: {media_names.get(media_type, '📝 Текст')}")
    if btn_text and btn_url:
        lines.append(f"🔘 Кнопка: {btn_text} → {btn_url}")
    if poll_json:
        try:
            _p = json.loads(poll_json)
            tags = []
            tags.append("анонимный" if _p.get("is_anonymous", True) else "публичный")
            if _p.get("allows_multiple_answers", False):
                tags.append("мультивыбор")
            lines.append(
                f"📊 Опрос: {_p['question']} ({len(_p['options'])} вар., "
                f"{', '.join(tags)})"
            )
        except Exception:
            pass
    body = text or "<i>(без текста)</i>"
    if len(body) > 500:
        body = body[:500] + "…"
    lines.append(f"\n<b>Текст:</b>\n{body}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("postcard:"))
async def cb_postcard(c: CallbackQuery, state: FSMContext):
    await state.clear()
    _, cid, pid = c.data.split(":")
    post = await db.get_post(int(pid))
    if not post or post[1] != int(cid):
        await c.answer("Пост не найден (возможно, уже опубликован или удалён).", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=await kb.posts_menu_kb(int(cid)))
        return
    await c.message.edit_text(
        _format_post_card(post),
        reply_markup=kb.post_card_kb(int(cid), int(pid)),
        disable_web_page_preview=True,
    )
    await c.answer()


@router.callback_query(F.data.startswith("editposttext:"))
async def cb_edit_post_text(c: CallbackQuery, state: FSMContext):
    _, cid, pid = c.data.split(":")
    await state.update_data(edit_chat_id=int(cid), edit_post_id=int(pid))
    await state.set_state(Form.edit_post_text)
    await c.message.edit_text(
        "✏️ Пришли новый текст поста.\n"
        "(Для постов с медиа это будет подпись.)",
        reply_markup=kb.back_kb(f"postcard:{cid}:{pid}")
    )
    await c.answer()


@router.message(Form.edit_post_text)
async def in_edit_post_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    pid = data["edit_post_id"]
    cid = data["edit_chat_id"]
    await db.update_post_text(pid, message.html_text)
    await state.clear()
    post = await db.get_post(pid)
    if post:
        await message.answer(
            _format_post_card(post),
            reply_markup=kb.post_card_kb(cid, pid),
            disable_web_page_preview=True,
        )
    else:
        await message.answer("✅ Текст обновлён.", reply_markup=await kb.posts_menu_kb(cid))


@router.callback_query(F.data.startswith("editposttime:"))
async def cb_edit_post_time(c: CallbackQuery, state: FSMContext):
    _, cid, pid = c.data.split(":")
    await state.update_data(edit_chat_id=int(cid), edit_post_id=int(pid))
    await state.set_state(Form.edit_post_time)
    await c.message.edit_text(
        f"🕓 Пришли новые дату и время: ДД.ММ.ГГГГ ЧЧ:ММ\n"
        f"Например: {datetime.now(TZ).strftime('%d.%m.%Y')} 20:00 (UTC+{TIMEZONE_OFFSET})",
        reply_markup=kb.back_kb(f"postcard:{cid}:{pid}")
    )
    await c.answer()


@router.message(Form.edit_post_time)
async def in_edit_post_time(message: Message, state: FSMContext):
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
    pid = data["edit_post_id"]
    cid = data["edit_chat_id"]
    await db.update_post_time(pid, pub)
    await state.clear()
    post = await db.get_post(pid)
    if post:
        await message.answer(
            _format_post_card(post),
            reply_markup=kb.post_card_kb(cid, pid),
            disable_web_page_preview=True,
        )
    else:
        await message.answer("✅ Время обновлено.", reply_markup=await kb.posts_menu_kb(cid))

# --- редактирование повтора ---
@router.callback_query(F.data.startswith("editpostrep:"))
async def cb_edit_post_rep(c: CallbackQuery):
    _, cid, pid = c.data.split(":")
    await c.message.edit_text(
        "🔁 Выбери режим повтора:",
        reply_markup=kb.post_repeat_edit_kb(int(cid), int(pid))
    )
    await c.answer()


@router.callback_query(F.data.startswith("setpostrep:"))
async def cb_set_post_rep(c: CallbackQuery):
    _, cid, pid, mode = c.data.split(":")
    await db.update_post_repeat(int(pid), mode)
    post = await db.get_post(int(pid))
    if post:
        await c.message.edit_text(
            _format_post_card(post),
            reply_markup=kb.post_card_kb(int(cid), int(pid)),
            disable_web_page_preview=True,
        )
    await c.answer("Повтор обновлён")


# --- редактирование кнопки ---
@router.callback_query(F.data.startswith("editpostbtn:"))
async def cb_edit_post_btn(c: CallbackQuery, state: FSMContext):
    _, cid, pid = c.data.split(":")
    await state.update_data(edit_chat_id=int(cid), edit_post_id=int(pid))
    await state.set_state(Form.edit_post_button)
    await c.message.edit_text(
        "🔘 Пришли кнопку в формате: Название | https://ссылка\n"
        "Или напиши <code>нет</code>, чтобы убрать кнопку.",
        reply_markup=kb.back_kb(f"postcard:{cid}:{pid}")
    )
    await c.answer()


@router.message(Form.edit_post_button)
async def in_edit_post_button(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    pid = data["edit_post_id"]
    cid = data["edit_chat_id"]
    raw = (message.text or "").strip()

    if raw.lower() == "нет":
        await db.update_post_button(pid, None, None)
    else:
        if "|" not in raw:
            await message.answer("Формат: Название | https://ссылка  (или «нет»)")
            return
        bt, bu = [p.strip() for p in raw.split("|", 1)]
        if not (bu.startswith("http://") or bu.startswith("https://")):
            await message.answer("Ссылка должна начинаться с http:// или https://")
            return
        await db.update_post_button(pid, bt, bu)

    await state.clear()
    post = await db.get_post(pid)
    if post:
        await message.answer(
            _format_post_card(post),
            reply_markup=kb.post_card_kb(cid, pid),
            disable_web_page_preview=True,
        )
    else:
        await message.answer("✅ Кнопка обновлена.", reply_markup=await kb.posts_menu_kb(cid))

# ============================================================
#        РЕДАКТИРОВАНИЕ ОПУБЛИКОВАННЫХ ПОСТОВ В КАНАЛЕ
# ============================================================
@router.callback_query(F.data.startswith("editpub:"))
async def cb_editpub(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "✏️ <b>Редактирование опубликованных</b>\n"
        "Можно изменить текст/подпись опубликованного поста. "
        "Само медиа и фото в альбоме заменить нельзя.\n\n"
        "Выбери пост:",
        reply_markup=await kb.editpub_list_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("editpubcard:"))
async def cb_editpub_card(c: CallbackQuery, state: FSMContext):
    await state.clear()
    _, cid, pid = c.data.split(":")
    post = await db.get_post(int(pid))
    if not post or post[1] != int(cid) or post[9] is None:
        await c.answer("Пост недоступен для редактирования.", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=await kb.editpub_list_kb(int(cid)))
        return
    media_type = post[6]
    when = datetime.fromtimestamp(post[5], TZ).strftime("%d.%m.%Y %H:%M")
    media_names = {"photo": "🖼 Фото", "video": "🎬 Видео",
                   "document": "📎 Документ", "album": "🗂 Альбом"}
    body = post[2] or "<i>(без текста)</i>"
    if len(body) > 800:
        body = body[:800] + "…"
    await c.message.edit_text(
        f"📄 <b>Опубликованный пост от {when}</b>\n"
        f"📦 Тип: {media_names.get(media_type, '📝 Текст')}\n\n"
        f"<b>Текущий текст:</b>\n{body}",
        reply_markup=kb.editpub_card_kb(int(cid), int(pid)),
        disable_web_page_preview=True,
    )
    await c.answer()


@router.callback_query(F.data.startswith("editpubtext:"))
async def cb_editpub_text(c: CallbackQuery, state: FSMContext):
    _, cid, pid = c.data.split(":")
    await state.update_data(pub_chat_id=int(cid), pub_post_id=int(pid))
    await state.set_state(Form.edit_pub_text)
    await c.message.edit_text(
        "✏️ Пришли новый текст (для медиа — новую подпись).",
        reply_markup=kb.back_kb(f"editpubcard:{cid}:{pid}")
    )
    await c.answer()


@router.message(Form.edit_pub_text)
async def in_editpub_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    pid = data["pub_post_id"]
    cid = data["pub_chat_id"]
    post = await db.get_post(pid)
    await state.clear()
    if not post or post[9] is None:
        await message.answer("Пост недоступен для редактирования.",
                             reply_markup=await kb.editpub_list_kb(cid))
        return

    new_text = message.html_text
    media_type = post[6]
    btn_text, btn_url = post[3], post[4]
    sent_id = post[9]

    ok = await _edit_published(message.bot, cid, sent_id, new_text,
                               media_type, btn_text, btn_url)
    if ok:
        await db.update_post_text(pid, new_text)  # синхронизируем БД
        await message.answer("✅ Опубликованный пост обновлён.",
                             reply_markup=await kb.editpub_list_kb(cid))
    else:
        await message.answer(
            "⚠️ Не удалось обновить (сообщение удалено, слишком старое или нет прав).",
            reply_markup=await kb.editpub_list_kb(cid)
        )

@router.callback_query(F.data.startswith("delpost:"))
async def cb_delpost(c: CallbackQuery):
    _, cid, pid = c.data.split(":")
    await c.message.edit_text(
        "🗑 Удалить этот отложенный пост?",
        reply_markup=kb.post_delete_confirm_kb(int(cid), int(pid))
    )
    await c.answer()


@router.callback_query(F.data.startswith("delpostok:"))
async def cb_delpost_ok(c: CallbackQuery):
    _, cid, pid = c.data.split(":")
    await db.cancel_post(int(pid), int(cid))
    await c.message.edit_text(
        "📝 <b>Отложенные посты</b>\nНажми на пост, чтобы открыть.",
        reply_markup=await kb.posts_menu_kb(int(cid))
    )
    await c.answer("Удалено")
