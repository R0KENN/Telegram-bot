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
    "auto_approve": "1",
    "welcome_text": "Привет! 👋 Рады видеть тебя в нашем сообществе.\n\n"
                    "Загляни в <b>закреплённые сообщения</b>.",
    "welcome_delay": "120",
    "rules_url": "https://t.me/",
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Проверь файл .env")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID не задан. Проверь файл .env")
