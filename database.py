import os
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

        # --- миграция: медиа, повтор, id опубликованного и опрос в постах ---
        async with db.execute("PRAGMA table_info(posts)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if "media_type" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN media_type TEXT")
        if "media_id" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN media_id TEXT")
        if "repeat_mode" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN repeat_mode TEXT DEFAULT 'once'")
        if "sent_message_id" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN sent_message_id INTEGER")
        if "poll_json" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN poll_json TEXT")

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_members_chat_joined "
            "ON members(chat_id, joined_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_status_time "
            "ON posts(status, publish_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_chat_status "
            "ON posts(chat_id, status)"
        )

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
    """Сбрасывает настройки чата, но сохраняет привязку к теме логов."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM settings WHERE chat_id=? AND key != 'log_thread_id'",
            (chat_id,)
        )
        await db.commit()


async def is_auto_approve(chat_id):
    return await get_setting(chat_id, "auto_approve") == "1"


# ====== УЧАСТНИКИ ======
async def save_member(chat_id, user_id, full_name, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO members (chat_id, user_id, full_name, username, joined_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "full_name=excluded.full_name, username=excluded.username",
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
                   media_type=None, media_id=None, repeat_mode="once",
                   poll_json=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO posts (chat_id, text, btn_text, btn_url, publish_at, status, "
            "media_type, media_id, repeat_mode, poll_json) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (chat_id, text, btn_text, btn_url, publish_at,
             media_type, media_id, repeat_mode, poll_json),
        )
        await db.commit()


async def get_pending_posts(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, publish_at, media_type, repeat_mode FROM posts "
            "WHERE chat_id=? AND status='pending' ORDER BY publish_at", (chat_id,)
        ) as cur:
            return await cur.fetchall()


async def get_due_posts():
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, chat_id, text, btn_text, btn_url, media_type, media_id, "
            "repeat_mode, poll_json "
            "FROM posts WHERE status='pending' AND publish_at<=?", (now,)
        ) as cur:
            return await cur.fetchall()


async def mark_post(post_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        await db.commit()

async def set_post_sent_id(post_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET sent_message_id=? WHERE id=?", (message_id, post_id)
        )
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
            "media_type, media_id, repeat_mode, sent_message_id, poll_json "
            "FROM posts WHERE id=?", (post_id,)
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

async def update_post_repeat(post_id, repeat_mode):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET repeat_mode=? WHERE id=?", (repeat_mode, post_id))
        await db.commit()


async def update_post_button(post_id, btn_text, btn_url):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET btn_text=?, btn_url=? WHERE id=?",
            (btn_text, btn_url, post_id)
        )
        await db.commit()


async def get_published_posts(chat_id, limit=20):
    """Опубликованные посты, у которых сохранён message_id (можно редактировать)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, publish_at, media_type, sent_message_id FROM posts "
            "WHERE chat_id=? AND status='published' AND sent_message_id IS NOT NULL "
            "ORDER BY publish_at DESC LIMIT ?",
            (chat_id, limit)
        ) as cur:
            return await cur.fetchall()

async def count_posts_by_status(chat_id):
    """Возвращает dict {status: count} для постов чата."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) FROM posts WHERE chat_id=? GROUP BY status",
            (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
    return {status: cnt for status, cnt in rows}

async def delete_old_posts(days=30):
    """Удаляет завершённые посты (published/cancelled/failed) старше days дней.
    Возвращает число удалённых записей. pending не трогаем."""
    cutoff = int(time.time()) - days * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM posts WHERE status IN ('published','cancelled','failed') "
            "AND publish_at < ?",
            (cutoff,)
        )
        await db.commit()
        return cur.rowcount


async def delete_finished_posts(chat_id):
    """Удаляет ВСЕ завершённые посты конкретного чата (по кнопке). Возвращает число."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM posts WHERE chat_id=? AND "
            "status IN ('published','cancelled','failed')",
            (chat_id,)
        )
        await db.commit()
        return cur.rowcount


async def get_finished_posts(chat_id, limit=20):
    """История завершённых постов чата (для просмотра)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, publish_at, status, media_type, repeat_mode FROM posts "
            "WHERE chat_id=? AND status IN ('published','cancelled','failed') "
            "ORDER BY publish_at DESC LIMIT ?",
            (chat_id, limit)
        ) as cur:
            return await cur.fetchall()


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
            "DELETE FROM topics WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id)
        )
        await db.execute(
            "DELETE FROM topics WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id)
        )
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


# ====== ГЛОБАЛЬНАЯ ПАНЕЛЬ СТАТУСА ======
async def count_chats_by_type():
    """Возвращает dict {'channel': N, 'group': M} по всем реальным чатам."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT type, COUNT(*) FROM chats WHERE chat_id != 0 GROUP BY type"
        ) as cur:
            rows = await cur.fetchall()
    result = {"channel": 0, "group": 0}
    for ctype, cnt in rows:
        result[ctype] = cnt
    return result


async def count_pending_posts_all():
    """Сколько всего постов в очереди по всем чатам."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM posts WHERE status='pending'"
        ) as cur:
            return (await cur.fetchone())[0]


async def count_chats_with_log_thread():
    """Сколько управляемых чатов имеют привязанную тему логов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM settings s "
            "WHERE s.key='log_thread_id' AND s.value != '' AND s.chat_id != 0 "
            "AND EXISTS (SELECT 1 FROM chats c WHERE c.chat_id = s.chat_id)"
        ) as cur:
            return (await cur.fetchone())[0]


# ====== БЭКАП ======
async def make_backup(dest_path):
    """Делает целостную копию БД в dest_path (безопасно при WAL)."""
    import os
    # VACUUM INTO требует, чтобы целевого файла не существовало
    if os.path.exists(dest_path):
        os.remove(dest_path)
    if os.path.exists(dest_path):
        os.remove(dest_path)
    async with aiosqlite.connect(DB_PATH) as db:
        # VACUUM INTO создаёт консистентную копию, не мешая рабочей базе
        await db.execute("VACUUM INTO ?", (dest_path,))
        await db.commit()