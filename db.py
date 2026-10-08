"""
db.py - لایه دیتابیس ربات کوییز، فقط با sqlite3 استاندارد پایتون.

عمداً هیچ ابزار ORM (مثل SQLAlchemy) استفاده نشده تا نصب روی گوشی/Termux
هیچ نیازی به کامپایل هیچ پکیجی نداشته باشد - sqlite3 همیشه همراه خود
پایتون است.

همه‌ی توابع Thread-safe هستند (یک قفل سراسری روی هر عملیات نوشتن/خواندن)
چون ربات هر Update تلگرام را در یک Thread جدا پردازش می‌کند.
"""
from __future__ import annotations

import os
import random
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("DATABASE_PATH", "quiz.db")

ARCHIVE_TOPIC_TITLES = {
    "previous_terms": "کل ورکشاپ",
    "first_term": "کل ترم اول",
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
    return _conn


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS score_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                delta REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id INTEGER NOT NULL REFERENCES sections(id),
                name TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_archive INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL REFERENCES topics(id),
                text TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                source_question_id INTEGER REFERENCES questions(id),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL REFERENCES questions(id),
                label TEXT NOT NULL,
                text TEXT NOT NULL,
                is_correct INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER REFERENCES topics(id),
                status TEXT NOT NULL,
                question_time_seconds INTEGER NOT NULL DEFAULT 15,
                question_count INTEGER NOT NULL DEFAULT 5,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                announce_message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS quiz_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
                question_id INTEGER NOT NULL REFERENCES questions(id),
                order_index INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL,
                message_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                is_finished INTEGER NOT NULL DEFAULT 0,
                UNIQUE(quiz_id, order_index)
            );

            CREATE TABLE IF NOT EXISTS quiz_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                joined_at TEXT NOT NULL,
                total_score REAL NOT NULL DEFAULT 0,
                UNIQUE(quiz_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
                quiz_question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                selected_option_id INTEGER NOT NULL REFERENCES options(id),
                is_correct INTEGER NOT NULL,
                response_time REAL NOT NULL,
                score REAL NOT NULL,
                timestamp TEXT NOT NULL,
                UNIQUE(quiz_question_id, user_id)
            );
            """
        )
        conn.commit()
        _migrate_schema(conn)
        _seed_sections(conn)
        _seed_archive_topics(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """برای دیتابیس‌هایی که با نسخه‌ی قدیمی‌تر ساخته شده‌اند - چون
    CREATE TABLE IF NOT EXISTS ستون تازه را به جدول از قبل موجود اضافه
    نمی‌کند، اینجا دستی چک و اضافه می‌شود."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(questions)")}
    if "source_question_id" not in cols:
        conn.execute("ALTER TABLE questions ADD COLUMN source_question_id INTEGER")
        conn.commit()


def _seed_sections(conn: sqlite3.Connection) -> None:
    existing = {r["key"] for r in conn.execute("SELECT key FROM sections")}
    to_add = []
    if "previous_terms" not in existing:
        to_add.append(("previous_terms", "🪄 ورکشاپ"))
    if "first_term" not in existing:
        to_add.append(("first_term", "🔮 ترم اول"))
    if to_add:
        conn.executemany("INSERT INTO sections (key, title) VALUES (?, ?)", to_add)
        conn.commit()


def _seed_archive_topics(conn: sqlite3.Connection) -> None:
    for key, title in ARCHIVE_TOPIC_TITLES.items():
        section = conn.execute("SELECT id FROM sections WHERE key=?", (key,)).fetchone()
        if not section:
            continue
        existing = conn.execute(
            "SELECT id FROM topics WHERE section_id=? AND is_archive=1", (section["id"],)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO topics (section_id, name, display_order, is_active, is_archive, created_at) "
                "VALUES (?,?,-1,1,1,?)",
                (section["id"], title, _now()),
            )
    conn.commit()


# --------------------------------------------------------------------- #
# Admins - چند ادمین همزمان، ذخیره در دیتابیس تا بدون ادیت کردن .env هم
# بشود ادمین اضافه/حذف کرد. علاوه بر این‌ها، ADMIN_IDS در .env همیشه به‌عنوان
# «ادمین‌های ثابت» معتبرند (چک‌شان در bot.py انجام می‌شود).
# --------------------------------------------------------------------- #

def seed_admins_from_env(user_ids: set[int]) -> None:
    with _lock:
        conn = get_conn()
        for uid in user_ids:
            conn.execute(
                "INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?,?)", (uid, _now())
            )
        conn.commit()


def is_admin_id(user_id: int) -> bool:
    return get_conn().execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def add_admin(user_id: int) -> bool:
    with _lock:
        conn = get_conn()
        if is_admin_id(user_id):
            return False
        conn.execute("INSERT INTO admins (user_id, added_at) VALUES (?,?)", (user_id, _now()))
        conn.commit()
        return True


def remove_admin(user_id: int) -> None:
    with _lock:
        get_conn().execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        get_conn().commit()


def list_admin_ids() -> list[int]:
    return [r["user_id"] for r in get_conn().execute("SELECT user_id FROM admins ORDER BY added_at")]


# --------------------------------------------------------------------- #
# Settings - تنظیمات عمومی ربات (کلید/مقدار ساده)، از داخل تلگرام قابل تغییر
# --------------------------------------------------------------------- #

def get_setting(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        get_conn().execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value))
        )
        get_conn().commit()


DEFAULT_SCORE_CONFIG: dict[int, tuple[float, float, float]] = {
    1: (20.0, 1.0, 8.0),
    5: (4.0, 0.5, 1.0),
    10: (2.0, 0.5, 1.0),
    15: (20.0 / 15.0, 0.25, 0.5),
    20: (1.0, 0.25, 0.5),
}


def get_score_config(question_count: int) -> tuple[float, float, float]:
    """(نمره‌ی پایه، جریمه‌ی کوچک/کمینه، جریمه‌ی بزرگ/بیشینه) برای این تعداد سؤال.
    اگر ادمین از تنظیمات تغییرش نداده باشد، مقدار پیش‌فرض برگردانده می‌شود."""
    base_d, low_d, high_d = DEFAULT_SCORE_CONFIG.get(
        question_count, (20.0 / max(1, question_count), 0.25, 0.5)
    )
    base = float(get_setting(f"score_{question_count}_base", base_d))
    low = float(get_setting(f"score_{question_count}_low", low_d))
    high = float(get_setting(f"score_{question_count}_high", high_d))
    return base, low, high


def set_score_config(question_count: int, base: float, low: float, high: float) -> None:
    set_setting(f"score_{question_count}_base", base)
    set_setting(f"score_{question_count}_low", low)
    set_setting(f"score_{question_count}_high", high)


# --------------------------------------------------------------------- #
# مدیریت دستی امتیازات - جدول score_adjustments جدا از تاریخچه‌ی واقعی
# کوییزها نگه داشته می‌شود؛ یعنی وقتی ادمین امتیاز کسی را دستی عوض می‌کند،
# رکوردهای quiz_participants (سوابق واقعی هر کوییز) دست‌نخورده می‌مانند و
# فقط مجموع نهایی (رتبه‌بندی کلی) اصلاح می‌شود.
# --------------------------------------------------------------------- #

def add_score_adjustment(user_id: int, delta: float) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO score_adjustments (user_id, delta, created_at) VALUES (?,?,?)",
            (user_id, delta, _now()),
        )
        get_conn().commit()


def get_user_total_score(user_id: int) -> float:
    conn = get_conn()
    base = conn.execute(
        "SELECT COALESCE(SUM(total_score),0) AS t FROM quiz_participants WHERE user_id=?", (user_id,)
    ).fetchone()["t"]
    adj = conn.execute(
        "SELECT COALESCE(SUM(delta),0) AS t FROM score_adjustments WHERE user_id=?", (user_id,)
    ).fetchone()["t"]
    return round(base + adj, 4)


def zero_out_user_score(user_id: int) -> None:
    current = get_user_total_score(user_id)
    if current != 0:
        add_score_adjustment(user_id, -current)


def remove_user_from_leaderboard(user_id: int) -> None:
    """کاربر را کاملاً از رتبه‌بندی کلی و از لیست شرکت‌کنندگان همه‌ی کوییزها حذف می‌کند."""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM quiz_participants WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM score_adjustments WHERE user_id=?", (user_id,))
        conn.commit()


# --------------------------------------------------------------------- #
# Users - Telegram user_id همیشه کلید اصلی است، هرگز username
# --------------------------------------------------------------------- #

def get_or_create_user(user_id: int, display_name: str, username: str | None) -> None:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (id, username, display_name, created_at) VALUES (?,?,?,?)",
                (user_id, username, display_name, _now()),
            )
            conn.commit()
            return
        if row["display_name"] != display_name or row["username"] != username:
            conn.execute(
                "UPDATE users SET display_name=?, username=? WHERE id=?",
                (display_name, username, user_id),
            )
            conn.commit()


def get_user(user_id: int) -> sqlite3.Row | None:
    return get_conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


# --------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------- #

def list_sections() -> list[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM sections ORDER BY id").fetchall()


def get_section_by_key(key: str) -> sqlite3.Row | None:
    return get_conn().execute("SELECT * FROM sections WHERE key=?", (key,)).fetchone()


def get_section(section_id: int) -> sqlite3.Row | None:
    return get_conn().execute("SELECT * FROM sections WHERE id=?", (section_id,)).fetchone()


# --------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------- #

def list_topics(section_id: int, active_only: bool = False) -> list[sqlite3.Row]:
    conn = get_conn()
    sql = "SELECT * FROM topics WHERE section_id=?"
    if active_only:
        sql += " AND is_active=1"
    sql += " ORDER BY display_order, id"
    return conn.execute(sql, (section_id,)).fetchall()


def get_topic(topic_id: int) -> sqlite3.Row | None:
    return get_conn().execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()


def get_archive_topic(section_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM topics WHERE section_id=? AND is_archive=1", (section_id,)
    ).fetchone()


def add_topic(section_id: int, name: str) -> int:
    with _lock:
        conn = get_conn()
        max_order = conn.execute(
            "SELECT MAX(display_order) AS m FROM topics WHERE section_id=?", (section_id,)
        ).fetchone()["m"] or 0
        cur = conn.execute(
            "INSERT INTO topics (section_id, name, display_order, is_active, is_archive, created_at) "
            "VALUES (?,?,?,1,0,?)",
            (section_id, name, max_order + 1, _now()),
        )
        conn.commit()
        return cur.lastrowid


def rename_topic(topic_id: int, new_name: str) -> bool:
    with _lock:
        topic = get_topic(topic_id)
        if not topic or topic["is_archive"]:
            return False
        get_conn().execute("UPDATE topics SET name=? WHERE id=?", (new_name, topic_id))
        get_conn().commit()
        return True


def delete_topic(topic_id: int) -> bool:
    with _lock:
        topic = get_topic(topic_id)
        if not topic or topic["is_archive"]:
            return False
        conn = get_conn()
        q_ids = [r["id"] for r in conn.execute("SELECT id FROM questions WHERE topic_id=?", (topic_id,))]
        copy_ids: list[int] = []
        for qid in q_ids:
            # کپی‌های آرشیوی این سؤال‌ها (فرزندها) هم باید حذف شوند، وگرنه
            # توی مبحث آرشیوی یتیم و قدیمی باقی می‌مانند.
            copies = conn.execute("SELECT id FROM questions WHERE source_question_id=?", (qid,)).fetchall()
            copy_ids.extend(c["id"] for c in copies)

        # ترتیب مهم است: فرزندها (کپی‌های آرشیوی) باید قبل از پدرها (سؤالات
        # اصلی همین مبحث) حذف شوند، وگرنه کلید خارجی source_question_id خطا می‌دهد.
        for qid in copy_ids:
            conn.execute("DELETE FROM options WHERE question_id=?", (qid,))
            conn.execute("DELETE FROM questions WHERE id=?", (qid,))
        for qid in q_ids:
            conn.execute("DELETE FROM options WHERE question_id=?", (qid,))
            conn.execute("DELETE FROM questions WHERE id=?", (qid,))

        conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        conn.commit()
        return True


def set_topic_active(topic_id: int, is_active: bool) -> None:
    with _lock:
        get_conn().execute("UPDATE topics SET is_active=? WHERE id=?", (int(is_active), topic_id))
        get_conn().commit()


def move_topic(topic_id: int, direction: int) -> None:
    with _lock:
        conn = get_conn()
        topic = get_topic(topic_id)
        if not topic or topic["is_archive"]:
            return
        siblings = [t for t in list_topics(topic["section_id"]) if not t["is_archive"]]
        idx = next((i for i, t in enumerate(siblings) if t["id"] == topic_id), None)
        if idx is None:
            return
        swap_idx = idx + direction
        if 0 <= swap_idx < len(siblings):
            other = siblings[swap_idx]
            conn.execute("UPDATE topics SET display_order=? WHERE id=?", (other["display_order"], topic["id"]))
            conn.execute("UPDATE topics SET display_order=? WHERE id=?", (topic["display_order"], other["id"]))
            conn.commit()


# --------------------------------------------------------------------- #
# Questions + Options
# --------------------------------------------------------------------- #

def _attach_options(question_row: sqlite3.Row) -> dict:
    conn = get_conn()
    opts = conn.execute(
        "SELECT * FROM options WHERE question_id=? ORDER BY label", (question_row["id"],)
    ).fetchall()
    return {
        "id": question_row["id"],
        "topic_id": question_row["topic_id"],
        "text": question_row["text"],
        "options": [dict(o) for o in opts],
    }


def list_questions(topic_id: int) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM questions WHERE topic_id=? AND is_active=1 ORDER BY id", (topic_id,)
    ).fetchall()
    return [_attach_options(r) for r in rows]


def get_question(question_id: int) -> dict | None:
    row = get_conn().execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    return _attach_options(row) if row else None


def _insert_question(conn: sqlite3.Connection, topic_id: int, text: str,
                      options: list[tuple[str, str]], correct_label: str,
                      source_question_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO questions (topic_id, text, is_active, source_question_id, created_at) VALUES (?,?,1,?,?)",
        (topic_id, text, source_question_id, _now()),
    )
    question_id = cur.lastrowid
    for label, opt_text in options:
        conn.execute(
            "INSERT INTO options (question_id, label, text, is_correct) VALUES (?,?,?,?)",
            (question_id, label, opt_text, int(label == correct_label)),
        )
    return question_id


def add_question(topic_id: int, text: str, options: list[tuple[str, str]], correct_label: str) -> int:
    """سؤال را در مبحث انتخاب‌شده ذخیره می‌کند، و اگر آن مبحث «آرشیو» نباشد،
    یک کپی از همین سؤال را خودکار در مبحث آرشیوِ همان بخش هم می‌گذارد (برای
    آزمون کلی نهایی). کپی با source_question_id به نسخه‌ی اصلی وصل می‌شود
    تا بعداً ویرایش/حذف هرکدام، خودکار روی طرف مقابل هم اعمال شود."""
    with _lock:
        conn = get_conn()
        topic = get_topic(topic_id)
        question_id = _insert_question(conn, topic_id, text, options, correct_label)
        if topic and not topic["is_archive"]:
            archive = get_archive_topic(topic["section_id"])
            if archive:
                _insert_question(conn, archive["id"], text, options, correct_label, source_question_id=question_id)
        conn.commit()
        return question_id


def _linked_question_ids(conn: sqlite3.Connection, question_id: int) -> list[int]:
    """آیدیِ نسخه‌ی «جفتِ» این سؤال را برمی‌گرداند: اگر خودش یک کپی آرشیوی
    است، آیدی نسخه‌ی اصلی؛ اگر خودش نسخه‌ی اصلی است، آیدی همه‌ی کپی‌های
    آرشیوی‌اش."""
    row = conn.execute(
        "SELECT source_question_id FROM questions WHERE id=?", (question_id,)
    ).fetchone()
    if not row:
        return []
    if row["source_question_id"]:
        return [row["source_question_id"]]
    copies = conn.execute(
        "SELECT id FROM questions WHERE source_question_id=?", (question_id,)
    ).fetchall()
    return [c["id"] for c in copies]


def update_question_text(question_id: int, text: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE questions SET text=? WHERE id=?", (text, question_id))
        for linked_id in _linked_question_ids(conn, question_id):
            conn.execute("UPDATE questions SET text=? WHERE id=?", (text, linked_id))
        conn.commit()


def delete_question(question_id: int) -> None:
    with _lock:
        conn = get_conn()
        linked = _linked_question_ids(conn, question_id)
        row = conn.execute(
            "SELECT source_question_id FROM questions WHERE id=?", (question_id,)
        ).fetchone()
        if row and row["source_question_id"]:
            # question_id خودش یک «کپیِ آرشیوی» است - به نسخه‌ی اصلی (parent)
            # اشاره می‌کند، پس خودش باید اول حذف شود (فرزند قبل از پدر).
            ordered_ids = [question_id] + linked
        else:
            # question_id «اصلی» است؛ کپی‌های آرشیوی (فرزندها) به آن اشاره
            # می‌کنند و باید قبل از خودِ آن حذف شوند، وگرنه به خاطر کلید
            # خارجیِ source_question_id خطا می‌دهد.
            ordered_ids = linked + [question_id]
        for qid in ordered_ids:
            conn.execute("DELETE FROM options WHERE question_id=?", (qid,))
            conn.execute("DELETE FROM questions WHERE id=?", (qid,))
        conn.commit()


def count_questions(topic_id: int) -> int:
    return get_conn().execute(
        "SELECT COUNT(*) AS c FROM questions WHERE topic_id=? AND is_active=1", (topic_id,)
    ).fetchone()["c"]


# --------------------------------------------------------------------- #
# Quizzes
# --------------------------------------------------------------------- #

def create_quiz(chat_id: int, created_by: int, topic_id: int,
                 question_time_seconds: int, question_count: int) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO quizzes (chat_id, topic_id, status, question_time_seconds, "
            "question_count, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (chat_id, topic_id, "waiting_for_players", question_time_seconds,
             question_count, created_by, _now()),
        )
        conn.commit()
        return cur.lastrowid


def get_quiz(quiz_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if row is None:
        return None
    quiz = dict(row)
    topic_name = None
    if quiz["topic_id"]:
        t = get_topic(quiz["topic_id"])
        topic_name = t["name"] if t else None
    quiz["topic_name"] = topic_name
    qqs = conn.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY order_index", (quiz_id,)
    ).fetchall()
    quiz["quiz_questions"] = [dict(q) for q in qqs]
    return quiz


def get_active_quiz_for_chat(chat_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM quizzes WHERE chat_id=? AND status NOT IN ('finished','cancelled') "
        "ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return get_quiz(row["id"]) if row else None


def list_recoverable_quizzes() -> list[int]:
    rows = get_conn().execute(
        "SELECT id FROM quizzes WHERE status IN ('running','question_active','question_finished')"
    ).fetchall()
    return [r["id"] for r in rows]


def set_status(quiz_id: int, status: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE quizzes SET status=? WHERE id=?", (status, quiz_id))
        if status == "running":
            row = conn.execute("SELECT started_at FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
            if row and row["started_at"] is None:
                conn.execute("UPDATE quizzes SET started_at=? WHERE id=?", (_now(), quiz_id))
        if status in ("finished", "cancelled"):
            conn.execute("UPDATE quizzes SET finished_at=? WHERE id=?", (_now(), quiz_id))
        conn.commit()


def set_announce_message(quiz_id: int, message_id: int) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE quizzes SET announce_message_id=? WHERE id=?", (message_id, quiz_id)
        )
        get_conn().commit()


def add_participant(quiz_id: int, user_id: int) -> bool:
    """True اگر تازه اضافه شد، False اگر قبلاً بوده."""
    with _lock:
        conn = get_conn()
        existing = conn.execute(
            "SELECT id FROM quiz_participants WHERE quiz_id=? AND user_id=?", (quiz_id, user_id)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO quiz_participants (quiz_id, user_id, joined_at, total_score) VALUES (?,?,?,0)",
            (quiz_id, user_id, _now()),
        )
        conn.commit()
        return True


def list_participants(quiz_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT p.*, u.display_name AS display_name, u.username AS username FROM quiz_participants p "
        "JOIN users u ON u.id=p.user_id WHERE p.quiz_id=? ORDER BY p.total_score DESC, p.joined_at",
        (quiz_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_participant(quiz_id: int, user_id: int) -> bool:
    return get_conn().execute(
        "SELECT id FROM quiz_participants WHERE quiz_id=? AND user_id=?", (quiz_id, user_id)
    ).fetchone() is not None


def build_quiz_questions(quiz_id: int, question_ids: list[int], duration_seconds: int) -> None:
    with _lock:
        conn = get_conn()
        for idx, qid in enumerate(question_ids):
            conn.execute(
                "INSERT INTO quiz_questions (quiz_id, question_id, order_index, duration_seconds, "
                "is_finished) VALUES (?,?,?,?,0)",
                (quiz_id, qid, idx, duration_seconds),
            )
        conn.commit()


def _attach_question_to_qq(qq_row: sqlite3.Row | dict) -> dict:
    qq = dict(qq_row)
    qq["question"] = get_question(qq["question_id"])
    return qq


def get_quiz_question_by_order(quiz_id: int, order_index: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM quiz_questions WHERE quiz_id=? AND order_index=?", (quiz_id, order_index)
    ).fetchone()
    return _attach_question_to_qq(row) if row else None


def get_quiz_question(quiz_question_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM quiz_questions WHERE id=?", (quiz_question_id,)
    ).fetchone()
    return _attach_question_to_qq(row) if row else None


def mark_question_started(quiz_question_id: int, message_id: int) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE quiz_questions SET started_at=?, message_id=? WHERE id=?",
            (_now(), message_id, quiz_question_id),
        )
        get_conn().commit()


def mark_question_finished(quiz_question_id: int) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE quiz_questions SET is_finished=1, finished_at=? WHERE id=?",
            (_now(), quiz_question_id),
        )
        get_conn().commit()


def record_answer(
    quiz_id: int,
    quiz_question_id: int,
    user_id: int,
    selected_option_id: int,
    is_correct: bool,
    response_time: float,
    score: float,
) -> bool:
    """False اگر قبلاً پاسخ داده بود (یا هم‌زمان یک پاسخ دیگر برنده شد) - Race-safe
    به لطف UNIQUE(quiz_question_id, user_id) واقعی در سطح دیتابیس.
    """
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO answers (quiz_id, quiz_question_id, user_id, selected_option_id, "
                "is_correct, response_time, score, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (quiz_id, quiz_question_id, user_id, selected_option_id, int(is_correct),
                 response_time, score, _now()),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        if score:
            conn.execute(
                "UPDATE quiz_participants SET total_score = total_score + ? WHERE quiz_id=? AND user_id=?",
                (score, quiz_id, user_id),
            )
        conn.commit()
        return True


def answers_for_question(quiz_question_id: int) -> list[dict]:
    rows = get_conn().execute(
        "SELECT a.*, u.display_name AS display_name FROM answers a "
        "JOIN users u ON u.id=a.user_id WHERE a.quiz_question_id=? ORDER BY a.timestamp",
        (quiz_question_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def overall_leaderboard(limit: int = 20) -> list[dict]:
    rows = get_conn().execute(
        """
        SELECT u.id AS user_id, u.display_name AS display_name,
               COALESCE(qp.total, 0) + COALESCE(adj.total, 0) AS total
        FROM users u
        LEFT JOIN (
            SELECT user_id, SUM(total_score) AS total FROM quiz_participants GROUP BY user_id
        ) qp ON qp.user_id = u.id
        LEFT JOIN (
            SELECT user_id, SUM(delta) AS total FROM score_adjustments GROUP BY user_id
        ) adj ON adj.user_id = u.id
        WHERE qp.total IS NOT NULL OR adj.total IS NOT NULL
        ORDER BY total DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_finished_quizzes(chat_id: int, limit: int = 15) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM quizzes WHERE chat_id=? AND status='finished' "
        "ORDER BY finished_at DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        t = get_topic(d["topic_id"]) if d["topic_id"] else None
        d["topic_name"] = t["name"] if t else "—"
        result.append(d)
    return result
