import os
from datetime import timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "3"))

DB_PATH = "bot.db"
BROADCAST_PAUSE = 0.1
TZ = timezone(timedelta(hours=TIMEZONE_OFFSET))

DEFAULTS = {
    "auto_approve": "0",          # автоприём выключен по умолчанию
    # --- приветствие в личку (каналы) ---
    "welcome_enabled": "1",
    "welcome_text": "Привет! 👋 Рады видеть тебя в нашем сообществе.",
    "welcome_delay": "120",
    # кнопки приветствия хранятся отдельной таблицей
    # --- модерация групп ---
    "del_links": "0",             # удаление ссылок от не-админов
    "word_filter": "0",           # фильтр запрещённых слов
    "clean_service": "0",         # удаление системных сообщений (вход/выход/закреп и т.п.)
    "group_welcome_enabled": "0", # приветствие в группе
    "group_welcome_text": "Добро пожаловать, {name}! 👋",
    "group_welcome_ttl": "60",    # через сколько секунд удалить приветствие
    # --- капча для новичков (только группы) ---
    "captcha_enabled": "0",       # капча выключена по умолчанию
    "captcha_timeout": "120",     # сколько секунд даётся на прохождение
    "captcha_action": "kick",     # что делать с непрошедшим: kick | mute
    # --- предупреждения ---
    "warn_mute_limit": "3",       # после скольки варнов мут
    "warn_ban_limit": "5",        # после скольки варнов бан
    "warn_mute_minutes": "60",    # на сколько минут мутить
    "warn_on_moderation": "1",    # выдавать варн при авто-удалении (ссылки/слова)
    # --- антифлуд ---
    "antiflood_enabled": "0",     # антифлуд выключен по умолчанию
    "antiflood_count": "5",       # сколько сообщений
    "antiflood_window": "7",      # за сколько секунд
    "antiflood_mute_minutes": "5",# на сколько минут мутить флудера

    "auto_reaction": "0",      # авто-реакция бота на посты в канале выключена по умолчанию
    "reaction_emoji": "🔥",    # какой эмодзи ставить
    "reaction_delay": "180",   # задержка перед реакцией в секундах (по умолчанию 3 минуты)
    "log_disabled": "0",          # логи в тему отключены вручную
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Проверь файл .env")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID не задан. Проверь файл .env")
