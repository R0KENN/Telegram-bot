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
                # Предупреждения пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warns (
                chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
    # --- миграция: медиа в постах ---
    async with db.execute("PRAGMA table_info(posts)") as cur:
        cols = [row[1] for row in await cur.fetchall()]
    if "media_type" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN media_type TEXT")
    if "media_id" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN media_id TEXT")
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
async def add_post(chat_id, text, btn_text, btn_url, publish_at,
                   media_type=None, media_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO posts (chat_id, text, btn_text, btn_url, publish_at, status, media_type, media_id) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (chat_id, text, btn_text, btn_url, publish_at, media_type, media_id),
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
            "SELECT id, chat_id, text, btn_text, btn_url, media_type, media_id, repeat_mode "
            "FROM posts WHERE status='pending' AND publish_at<=?", (now,)
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

async def reschedule_post(post_id, new_publish_at):
    """Переносит время публикации поста (для повторяющихся)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET publish_at=?, status='pending' WHERE id=?",
            (new_publish_at, post_id)
        )
        await db.commit()


async def get_post(post_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, chat_id, text, btn_text, btn_url, publish_at, "
            "media_type, media_id, repeat_mode FROM posts WHERE id=?", (post_id,)
        ) as cur:
            return await cur.fetchone()


async def update_post_text(post_id, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET text=? WHERE id=?", (text, post_id))
        await db.commit()


async def update_post_time(post_id, publish_at):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET publish_at=? WHERE id=?", (publish_at, post_id))
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


async def delete_chat(chat_id):
    """Полностью удаляет чат и все связанные с ним данные."""
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("settings", "members", "posts", "welcome_buttons",
                      "allowed_domains", "banned_words", "topics", "reacted_posts",
                      "warns"):
            await db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
        await db.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        await db.commit()

async def get_reacted_ids(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT message_id FROM reacted_posts WHERE chat_id=?", (chat_id,)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def unmark_reacted(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reacted_posts WHERE chat_id=? AND message_id=?",
            (chat_id, message_id)
        )
        await db.commit()


async def clear_reacted(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reacted_posts WHERE chat_id=?", (chat_id,))
        await db.commit()

# ====== ЛОГ-ГРУППА И ТЕМЫ ПО ЧАТАМ ======
# Глобальная лог-группа (одна на всё). Храним под служебным chat_id=0.
async def set_global_log_chat(log_chat_id):
    await set_setting(0, "global_log_chat", str(log_chat_id))


async def get_global_log_chat():
    val = await get_setting(0, "global_log_chat")
    return int(val) if val and val.lstrip("-").isdigit() else None


# Тема конкретного управляемого чата
async def set_log_thread(chat_id, thread_id):
    await set_setting(chat_id, "log_thread_id", str(thread_id))


async def get_log_thread(chat_id):
    val = await get_setting(chat_id, "log_thread_id")
    return int(val) if val and val.lstrip("-").isdigit() else None

async def delete_topics_by_thread(log_chat_id, thread_id):
    """Удаляет запись о теме из таблицы topics по thread_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM topics WHERE chat_id=? AND thread_id=?",
            (log_chat_id, thread_id)
        )
        await db.commit()

# ====== ПРЕДУПРЕЖДЕНИЯ ======
async def add_warn(chat_id, user_id):
    """Добавляет предупреждение и возвращает новое количество."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO warns (chat_id, user_id, count) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1",
            (chat_id, user_id)
        )
        await db.commit()
        async with db.execute(
            "SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_warns(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def remove_warn(chat_id, user_id):
    """Снимает одно предупреждение (не уходит ниже нуля). Возвращает новое количество."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE warns SET count = MAX(count - 1, 0) WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        )
        await db.commit()
        async with db.execute(
            "SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def reset_warns(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        await db.commit()

# ====== АНАЛИТИКА ======
async def get_join_timestamps(chat_id, since=None):
    """Возвращает список joined_at (unix) участников, опционально за период since..now."""
    q = "SELECT joined_at FROM members WHERE chat_id=?"
    params = [chat_id]
    if since is not None:
        q += " AND joined_at>=?"
        params.append(since)
    q += " ORDER BY joined_at"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(q, params) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_all_members(chat_id):
    """Полный список участников чата для выгрузки в CSV."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, full_name, username, joined_at FROM members "
            "WHERE chat_id=? ORDER BY joined_at", (chat_id,)
        ) as cur:
            return await cur.fetchall()
