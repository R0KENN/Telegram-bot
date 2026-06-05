import time

import aiosqlite

from config import DB_PATH, DEFAULTS


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                type TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS members (
                chat_id INTEGER, user_id INTEGER, full_name TEXT,
                username TEXT, joined_at INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER, key TEXT, value TEXT,
                PRIMARY KEY (chat_id, key)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, text TEXT, btn_text TEXT, btn_url TEXT,
                publish_at INTEGER, status TEXT DEFAULT 'pending'
            )
        """)
        # Кнопки приветствия (до 4 на чат)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS welcome_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, text TEXT, url TEXT
            )
        """)
        # Белый список доменов для фильтра ссылок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS allowed_domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, domain TEXT
            )
        """)
        # Запрещённые слова
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, word TEXT
            )
        """)
        # Запрещённые слова
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, word TEXT
            )
        """)
        # Темы форума (только созданные ботом)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, thread_id INTEGER, name TEXT
            )
        """)
                # Учёт постов, на которые бот уже поставил реакцию
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reacted_posts (
                chat_id INTEGER, message_id INTEGER,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        await db.commit()


# ====== ЧАТЫ ======
async def register_chat(chat_id, title, chat_type):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            exists = await cur.fetchone() is not None
        await db.execute(
            "INSERT OR REPLACE INTO chats (chat_id, title, type) VALUES (?, ?, ?)",
            (chat_id, title, chat_type)
        )
        if not exists:
            # Новый чат: автоприём выключен по умолчанию
            await db.execute(
                "INSERT OR IGNORE INTO settings (chat_id, key, value) "
                "VALUES (?, 'auto_approve', '0')",
                (chat_id,)
            )
        await db.commit()
        return not exists  # True, если чат новый


async def get_chats(chat_type=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if chat_type:
            q = "SELECT chat_id, title, type FROM chats WHERE type=?"
            params = (chat_type,)
        else:
            q = "SELECT chat_id, title, type FROM chats"
            params = ()
        async with db.execute(q, params) as cur:
            return await cur.fetchall()


async def get_chat(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, title, type FROM chats WHERE chat_id=?", (chat_id,)
        ) as cur:
            return await cur.fetchone()


# ====== НАСТРОЙКИ ======
async def get_setting(chat_id, key):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else DEFAULTS.get(key)


async def set_setting(chat_id, key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (chat_id, key, value) VALUES (?, ?, ?)",
            (chat_id, key, value)
        )
        await db.commit()


async def reset_settings(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM settings WHERE chat_id=?", (chat_id,))
        await db.commit()


async def is_auto_approve(chat_id):
    return await get_setting(chat_id, "auto_approve") == "1"


# ====== УЧАСТНИКИ ======
async def save_member(chat_id, user_id, full_name, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO members VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, full_name, username, int(time.time()))
        )
        await db.commit()


async def count_members(chat_id, since=None):
    q = "SELECT COUNT(*) FROM members WHERE chat_id=?"
    params = [chat_id]
    if since is not None:
        q += " AND joined_at>=?"
        params.append(since)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(q, params) as cur:
            return (await cur.fetchone())[0]


async def get_last_members(chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT full_name, username, joined_at FROM members "
            "WHERE chat_id=? ORDER BY joined_at DESC LIMIT ?", (chat_id, limit)
        ) as cur:
            return await cur.fetchall()


async def get_member_ids(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM members WHERE chat_id=?", (chat_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]



async def remove_members(chat_id, user_ids):
    """Удаляет из базы участников, которые заблокировали бота / недоступны."""
    if not user_ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "DELETE FROM members WHERE chat_id=? AND user_id=?",
            [(chat_id, uid) for uid in user_ids]
        )
        await db.commit()

# ====== ПОСТЫ ======
async def add_post(chat_id, text, btn_text, btn_url, publish_at):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO posts (chat_id, text, btn_text, btn_url, publish_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, text, btn_text, btn_url, publish_at)
        )
        await db.commit()


async def get_pending_posts(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, publish_at FROM posts "
            "WHERE chat_id=? AND status='pending' ORDER BY publish_at", (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def get_due_posts():
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, chat_id, text, btn_text, btn_url FROM posts "
            "WHERE status='pending' AND publish_at<=?", (now,)
        ) as cur:
            return await cur.fetchall()


async def mark_post(post_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        await db.commit()


async def cancel_post(post_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET status='cancelled' WHERE id=? AND chat_id=?",
            (post_id, chat_id)
        )
        await db.commit()


# ====== КНОПКИ ПРИВЕТСТВИЯ ======
async def get_welcome_buttons(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, url FROM welcome_buttons WHERE chat_id=? ORDER BY id",
            (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def add_welcome_button(chat_id, text, url):
    async with aiosqlite.connect(DB_PATH) as db:
        # ограничение: максимум 4 кнопки
        async with db.execute(
            "SELECT COUNT(*) FROM welcome_buttons WHERE chat_id=?", (chat_id,)
        ) as cur:
            if (await cur.fetchone())[0] >= 4:
                return False
        await db.execute(
            "INSERT INTO welcome_buttons (chat_id, text, url) VALUES (?, ?, ?)",
            (chat_id, text, url)
        )
        await db.commit()
        return True


async def delete_welcome_button(button_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM welcome_buttons WHERE id=? AND chat_id=?", (button_id, chat_id)
        )
        await db.commit()


# ====== БЕЛЫЙ СПИСОК ДОМЕНОВ ======
async def get_allowed_domains(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, domain FROM allowed_domains WHERE chat_id=?", (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def add_allowed_domain(chat_id, domain):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO allowed_domains (chat_id, domain) VALUES (?, ?)",
            (chat_id, domain.lower())
        )
        await db.commit()


async def delete_allowed_domain(domain_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM allowed_domains WHERE id=? AND chat_id=?", (domain_id, chat_id)
        )
        await db.commit()


# ====== ЗАПРЕЩЁННЫЕ СЛОВА ======
async def get_banned_words(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, word FROM banned_words WHERE chat_id=?", (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def add_banned_word(chat_id, word):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO banned_words (chat_id, word) VALUES (?, ?)",
            (chat_id, word.lower())
        )
        await db.commit()


async def delete_banned_word(word_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM banned_words WHERE id=? AND chat_id=?", (word_id, chat_id)
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, thread_id INTEGER, name TEXT
            )
        """)
        await db.commit()



# ====== ТЕМЫ ФОРУМА ======
async def add_topic(chat_id, thread_id, name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO topics (chat_id, thread_id, name) VALUES (?, ?, ?)",
            (chat_id, thread_id, name)
        )
        await db.commit()


async def get_topics(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, thread_id, name FROM topics WHERE chat_id=? ORDER BY id",
            (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def delete_topic(topic_db_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM topics WHERE id=? AND chat_id=?", (topic_db_id, chat_id)
        )
        await db.commit()
        
        

# ====== УЧЁТ РЕАКЦИЙ ======
async def is_reacted(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM reacted_posts WHERE chat_id=? AND message_id=?",
            (chat_id, message_id)
        ) as cur:
            return await cur.fetchone() is not None


async def mark_reacted(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reacted_posts (chat_id, message_id) VALUES (?, ?)",
            (chat_id, message_id)
        )
        await db.commit()


async def last_reacted_id(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT MAX(message_id) FROM reacted_posts WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else 0
