import asyncio
import time
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    ChatJoinRequest, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
import keyboards as kb
from config import ADMIN_ID, TZ, TIMEZONE_OFFSET, BROADCAST_PAUSE

router = Router()


class Form(StatesGroup):
    welcome_text = State()
    welcome_delay = State()
    rules_url = State()
    broadcast = State()
    post_text = State()
    post_button = State()
    post_time = State()


def is_admin_id(uid):
    return uid == ADMIN_ID


# ====== ЗАЯВКИ ======
@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest):
    bot = request.bot
    chat_id = request.chat.id
    await db.register_channel(chat_id, request.chat.title or str(chat_id))
    if not await db.is_auto_approve(chat_id):
        await notify_admin(bot, request, approved=False)
        return
    user = request.from_user
    try:
        await request.approve()
        await db.save_member(chat_id, user.id, user.full_name, user.username or "")
        await notify_admin(bot, request, approved=True)
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
            f"Имя: {user.full_name}\n"
            f"Username: @{user.username if user.username else '—'}\n"
            f"ID: <code>{user.id}</code>"
        )
    except Exception:
        pass


async def send_delayed_welcome(bot, chat_id, user_id):
    delay = int(await db.get_setting(chat_id, "welcome_delay"))
    await asyncio.sleep(delay)
    text = await db.get_setting(chat_id, "welcome_text")
    rules = await db.get_setting(chat_id, "rules_url")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Правила", url=rules)]
    ])
    try:
        await bot.send_message(user_id, text, reply_markup=keyboard)
    except Exception:
        pass


# ====== РЕГИСТРАЦИЯ КАНАЛА ПЕРЕСЫЛКОЙ ======
@router.message(F.forward_from_chat)
async def on_forward(message: Message):
    if not is_admin_id(message.from_user.id):
        return
    chat = message.forward_from_chat
    if chat.type != "channel":
        await message.answer("Это не канал. Перешли пост именно из канала.")
        return
    await db.register_channel(chat.id, chat.title or str(chat.id))
    await message.answer(
        f"✅ Канал «{chat.title}» добавлен.\nОткрой /menu, чтобы настроить.\n\n"
        f"<i>Сделай бота админом канала с правом «Добавлять участников».</i>"
    )


# ====== МЕНЮ ======
@router.message(Command("start"))
@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin_id(message.from_user.id):
        await message.answer("Бот для приёма заявок работает ✅")
        return
    await message.answer("<b>Твои каналы:</b>", reply_markup=await kb.channels_kb())


@router.callback_query(F.data == "channels")
async def cb_channels(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("<b>Твои каналы:</b>", reply_markup=await kb.channels_kb())
    await c.answer()


@router.callback_query(F.data == "addch")
async def cb_addch(c: CallbackQuery):
    await c.message.edit_text(
        "➕ <b>Добавление канала</b>\n\n"
        "1. Добавь бота админом в канал (право «Добавлять участников»).\n"
        "2. Перешли мне сюда любое сообщение из этого канала.",
        reply_markup=kb.back_kb("channels")
    )
    await c.answer()


@router.callback_query(F.data.startswith("ch:"))
async def cb_channel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    title = await db.get_channel_title(chat_id)
    await c.message.edit_text(
        f"📢 <b>{title}</b>\nЧто настроим?",
        reply_markup=await kb.channel_menu_kb(chat_id)
    )
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
        lines = ["🕓 <b>Последние вступившие:</b>\n"]
        for full_name, username, joined_at in rows:
            t = datetime.fromtimestamp(joined_at, TZ).strftime("%d.%m %H:%M")
            uname = f"@{username}" if username else "—"
            lines.append(f"• {full_name} ({uname}) — {t}")
        text = "\n".join(lines)
    await c.message.edit_text(text, reply_markup=kb.back_kb(f"ch:{chat_id}"))
    await c.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    new_state = not await db.is_auto_approve(chat_id)
    await db.set_setting(chat_id, "auto_approve", "1" if new_state else "0")
    title = await db.get_channel_title(chat_id)
    await c.message.edit_text(
        f"📢 <b>{title}</b>\nЧто настроим?",
        reply_markup=await kb.channel_menu_kb(chat_id)
    )
    await c.answer("Автоприём " + ("включён" if new_state else "выключен"))


@router.callback_query(F.data.startswith("reset:"))
async def cb_reset(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    await db.reset_settings(chat_id)
    title = await db.get_channel_title(chat_id)
    await c.message.edit_text(
        f"♻️ Настройки сброшены.\n\n📢 <b>{title}</b>\nЧто настроим?",
        reply_markup=await kb.channel_menu_kb(chat_id)
    )
    await c.answer("Сброшено")


# ====== ПРИВЕТСТВИЕ ======
@router.callback_query(F.data.startswith("wmenu:"))
async def cb_wmenu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text("⚙️ <b>Настройки приветствия</b>",
                              reply_markup=kb.welcome_menu_kb(chat_id))
    await c.answer()


@router.callback_query(F.data.startswith("show:"))
async def cb_show(c: CallbackQuery):
    chat_id = int(c.data.split(":")[1])
    text = await db.get_setting(chat_id, "welcome_text")
    delay = await db.get_setting(chat_id, "welcome_delay")
    rules = await db.get_setting(chat_id, "rules_url")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Правила", url=rules)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"wmenu:{chat_id}")],
    ])
    await c.message.edit_text(
        f"👁 <b>Приветствие</b> <i>(задержка: {delay} сек.)</i>\n\n{text}",
        reply_markup=keyboard
    )
    await c.answer()


@router.callback_query(F.data.startswith("st:"))
async def cb_set_text(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.welcome_text)
    await c.message.edit_text("✏️ Пришли новый текст приветствия.",
                              reply_markup=kb.back_kb(f"wmenu:{chat_id}"))
    await c.answer()


@router.callback_query(F.data.startswith("sd:"))
async def cb_set_delay(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.welcome_delay)
    await c.message.edit_text("⏱ Пришли задержку в секундах (0 — сразу).",
                              reply_markup=kb.back_kb(f"wmenu:{chat_id}"))
    await c.answer()


@router.callback_query(F.data.startswith("sr:"))
async def cb_set_rules(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.rules_url)
    await c.message.edit_text("🔗 Пришли ссылку (http:// или https://).",
                              reply_markup=kb.back_kb(f"wmenu:{chat_id}"))
    await c.answer()


@router.callback_query(F.data.startswith("bc:"))
async def cb_broadcast(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.broadcast)
    await c.message.edit_text("📨 Пришли сообщение для рассылки участникам канала.",
                              reply_markup=kb.back_kb(f"ch:{chat_id}"))
    await c.answer()


# ====== ПОСТЫ ======
@router.callback_query(F.data.startswith("posts:"))
async def cb_posts(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(c.data.split(":")[1])
    await c.message.edit_text(
        "📝 <b>Отложенные посты</b>\nНажми на пост, чтобы отменить.",
        reply_markup=await kb.posts_menu_kb(chat_id)
    )
    await c.answer()


@router.callback_query(F.data.startswith("newpost:"))
async def cb_newpost(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(chat_id=chat_id)
    await state.set_state(Form.post_text)
    await c.message.edit_text("📝 <b>Шаг 1 из 3.</b> Пришли текст поста.",
                              reply_markup=kb.back_kb(f"posts:{chat_id}"))
    await c.answer()


@router.message(Form.post_text)
async def input_post_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    await state.update_data(post_text=message.html_text)
    data = await state.get_data()
    await state.set_state(Form.post_button)
    await message.answer(
        "🔘 <b>Шаг 2 из 3.</b> Пришли кнопку в формате:\n"
        "<code>Название | https://ссылка</code>\nИли «Без кнопки».",
        reply_markup=kb.skip_kb(data["chat_id"])
    )


@router.callback_query(F.data.startswith("nobtn:"))
async def cb_nobtn(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.split(":")[1])
    await state.update_data(btn_text=None, btn_url=None)
    await state.set_state(Form.post_time)
    await c.message.edit_text(
        "🕓 <b>Шаг 3 из 3.</b> Пришли дату и время:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
        f"Например: <code>{datetime.now(TZ).strftime('%d.%m.%Y')} 20:00</code>\n"
        f"<i>Время по поясу UTC+{TIMEZONE_OFFSET}.</i>",
        reply_markup=kb.back_kb(f"posts:{chat_id}")
    )
    await c.answer()


@router.message(Form.post_button)
async def input_post_button(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if "|" not in message.text:
        await message.answer("Формат: Название | https://ссылка")
        return
    btn_text, btn_url = [p.strip() for p in message.text.split("|", 1)]
    if not (btn_url.startswith("http://") or btn_url.startswith("https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    await state.update_data(btn_text=btn_text, btn_url=btn_url)
    await state.set_state(Form.post_time)
    await message.answer(
        "🕓 <b>Шаг 3 из 3.</b> Пришли дату и время:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
        f"Например: <code>{datetime.now(TZ).strftime('%d.%m.%Y')} 20:00</code>\n"
        f"<i>Время по поясу UTC+{TIMEZONE_OFFSET}.</i>"
    )


@router.message(Form.post_time)
async def input_post_time(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await message.answer("Формат: 25.12.2026 20:00")
        return
    publish_at = int(dt.timestamp())
    if publish_at <= int(time.time()):
        await message.answer("Это время уже прошло.")
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    await db.add_post(chat_id, data["post_text"],
                      data.get("btn_text"), data.get("btn_url"), publish_at)
    await state.clear()
    await message.answer(
        f"✅ Пост запланирован на <b>{dt.strftime('%d.%m.%Y %H:%M')}</b>.",
        reply_markup=await kb.posts_menu_kb(chat_id)
    )


@router.callback_query(F.data.startswith("delpost:"))
async def cb_delpost(c: CallbackQuery):
    _, chat_id, pid = c.data.split(":")
    chat_id, pid = int(chat_id), int(pid)
    await db.cancel_post(pid, chat_id)
    await c.message.edit_text("📝 <b>Отложенные посты</b>\nПост отменён.",
                              reply_markup=await kb.posts_menu_kb(chat_id))
    await c.answer("Отменено")


# ====== ОСТАЛЬНОЙ ВВОД ======
@router.message(Form.welcome_text)
async def input_text(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    await db.set_setting(chat_id, "welcome_text", message.html_text)
    await state.clear()
    await message.answer("✅ Текст обновлён.", reply_markup=kb.welcome_menu_kb(chat_id))


@router.message(Form.welcome_delay)
async def input_delay(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    if not message.text.strip().isdigit():
        await message.answer("Нужно число.")
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    await db.set_setting(chat_id, "welcome_delay", message.text.strip())
    await state.clear()
    await message.answer(f"✅ Задержка {message.text.strip()} сек.",
                         reply_markup=kb.welcome_menu_kb(chat_id))


@router.message(Form.rules_url)
async def input_rules(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    url = message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    await db.set_setting(chat_id, "rules_url", url)
    await state.clear()
    await message.answer("✅ Ссылка обновлена.", reply_markup=kb.welcome_menu_kb(chat_id))


@router.message(Form.broadcast)
async def input_broadcast(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    await state.clear()
    text = message.html_text
    ids = await db.get_member_ids(chat_id)
    if not ids:
        await message.answer("Нет участников канала.",
                             reply_markup=await kb.channel_menu_kb(chat_id))
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
    await message.answer(
        f"📨 <b>Готово</b>\nДоставлено: {sent}\nНе доставлено: {failed}",
        reply_markup=await kb.channel_menu_kb(chat_id)
    )
