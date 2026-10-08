"""
bot.py - ربات کوییز الهوریس ✨

فقط با کتابخانه استاندارد پایتون کار می‌کند (urllib, sqlite3 از طریق
db.py, threading, json) - هیچ pip install ای برای کتابخانه‌های سنگین
(aiohttp/aiogram/SQLAlchemy) لازم نیست، بنابراین روی گوشی/Termux هم به
همان راحتی که روی سرور نصب می‌شود بالا می‌آید.

اجرا: python bot.py   (بعد از ساخت .env - نگاه کنید به README.md)
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import db

# --------------------------------------------------------------------- #
# تنظیمات - از .env خوانده می‌شود (بدون نیاز به python-dotenv)
# --------------------------------------------------------------------- #

def _load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN تنظیم نشده است. فایل .env.example را به .env کپی و پر کنید.")

# ادمین‌های «ثابت» - همیشه از طریق .env معتبرند و از داخل ربات قابل حذف نیستند.
# ادمین‌های بیشتر را می‌توان از داخل پنل مدیریت («👤 مدیریت ادمین‌ها») اضافه کرد؛
# آن‌ها در دیتابیس ذخیره می‌شوند و با ری‌استارت شدن ربات از بین نمی‌روند.
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip()
}
TIMER_UPDATE_INTERVAL = int(os.environ.get("TIMER_UPDATE_INTERVAL", "3"))
PAUSE_BETWEEN_QUESTIONS = 2.5
OFFSET_FILE = os.environ.get("OFFSET_FILE", "offset.txt")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("quizbot")

TIME_OPTIONS = [10, 15, 20, 30]
COUNT_OPTIONS = [1, 5, 10, 15, 20]
OPTION_LABELS = ["A", "B", "C", "D"]

# --------------------------------------------------------------------- #
# کلاینت مینیمال Telegram Bot API - فقط با urllib (بدون requests/aiohttp)
# --------------------------------------------------------------------- #

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}/"


def api(method: str, _request_timeout: int = 20, **params) -> dict | list | None:
    url = API_BASE + method
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=_request_timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            log.warning("HTTP error calling %s: %s", method, exc)
            return None
    except Exception as exc:
        log.warning("Network error calling %s: %s", method, exc)
        return None

    if not payload.get("ok"):
        if "not modified" not in str(payload.get("description", "")):
            log.debug("Telegram API error on %s: %s", method, payload)
        return None
    return payload.get("result")


def get_updates(offset: int, poll_timeout: int = 30) -> list:
    result = api(
        "getUpdates",
        _request_timeout=poll_timeout + 10,
        offset=offset,
        timeout=poll_timeout,
        allowed_updates=["message", "callback_query"],
    )
    return result or []


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict | None:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", **payload)


def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api("editMessageText", **payload)


def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict):
    return api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)


def delete_message(chat_id: int, message_id: int):
    return api("deleteMessage", chat_id=chat_id, message_id=message_id)


def answer_callback_query(callback_query_id: str, text: str | None = None, show_alert: bool = False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    return api("answerCallbackQuery", **payload)


# --------------------------------------------------------------------- #
# ادمین - فقط کسانی که واقعاً در «حافظه‌ی ربات» ادمین‌اند: یا در ADMIN_IDS
# ثابت (.env) هستند، یا از داخل پنل («👤 مدیریت ادمین‌ها») اضافه شده‌اند.
# عمداً از وضعیت «ادمین گروه تلگرام» استفاده نمی‌شود - چون ممکن است یک
# دانش‌آموز به هر دلیلی ادمین گروه باشد بدون این‌که قرار باشد به پنل
# مدیریت (و بانک سؤالات مباحث دیگر) دسترسی داشته باشد.
# --------------------------------------------------------------------- #

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or db.is_admin_id(user_id)


def display_name_of(user: dict) -> str:
    name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
    return name or user.get("username") or "کاربر"


def section_title(key: str) -> str:
    s = db.get_section_by_key(key)
    return s["title"] if s else key


# --------------------------------------------------------------------- #
# FSM ساده در حافظه - برای مراحل چندمرحله‌ای (افزودن مبحث/سؤال/ادمین)
# --------------------------------------------------------------------- #

_state_lock = threading.RLock()
_states: dict[tuple[int, int], dict] = {}


def set_state(chat_id: int, user_id: int, name: str, **data) -> None:
    with _state_lock:
        _states[(chat_id, user_id)] = {"name": name, **data}


def get_state(chat_id: int, user_id: int) -> dict | None:
    with _state_lock:
        return _states.get((chat_id, user_id))


def update_state(chat_id: int, user_id: int, **data) -> None:
    with _state_lock:
        st = _states.get((chat_id, user_id))
        if st is not None:
            st.update(data)


def clear_state(chat_id: int, user_id: int) -> None:
    with _state_lock:
        _states.pop((chat_id, user_id), None)


def pop_state_if(chat_id: int, user_id: int, expected_name: str) -> dict | None:
    """اگر state فعلی دقیقاً همان expected_name را داشته باشد، آن را atomically
    برمی‌دارد و حذف می‌کند (در یک قفل واحد) - برای جلوگیری از پردازش دوباره‌ی
    یک دکمه که با دو کلیک سریع پشت‌سرهم دوبار fire شده (مثلاً دوبار ذخیره
    شدن یک سؤال با دو بار زدن گزینه‌ی «صحیح»)."""
    with _state_lock:
        st = _states.get((chat_id, user_id))
        if st and st.get("name") == expected_name:
            del _states[(chat_id, user_id)]
            return st
        return None


def state_data(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "name"}


# --------------------------------------------------------------------- #
# کیبوردها (دیکشنری ساده مطابق فرمت Telegram Bot API)
# --------------------------------------------------------------------- #

def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def kb(*rows: list[dict]) -> dict:
    return {"inline_keyboard": list(rows)}


def main_admin_menu() -> dict:
    return kb(
        [btn("📚 مدیریت مباحث", "menu:topics_sections")],
        [btn("❓ مدیریت سوالات", "menu:questions_sections")],
        [btn("🏆 برگزاری کوییز", "menu:new_quiz")],
        [btn("👥 شرکت‌کنندگان", "menu:participants")],
        [btn("📊 آمار و امتیازات", "menu:stats")],
        [btn("🗂 سوابق کوییزها", "menu:history")],
        [btn("👤 مدیریت ادمین‌ها", "menu:admins")],
        [btn("⚙️ تنظیمات", "menu:settings")],
    )


def sections_menu(prefix: str) -> dict:
    rows = [[btn(s["title"], f"{prefix}:{s['key']}")] for s in db.list_sections()]
    rows.append([btn("🔙 بازگشت", "menu:main")])
    return kb(*rows)


def topics_management_menu(topics: list, section_key: str) -> dict:
    rows = []
    for t in topics:
        icon = "📦" if t["is_archive"] else ("✅" if t["is_active"] else "🚫")
        rows.append([btn(f"{icon} {t['name']}", f"topic:open:{t['id']}")])
    rows.append([btn("➕ افزودن مبحث جدید", f"topic:add:{section_key}")])
    rows.append([btn("🔙 بازگشت", "menu:topics_sections")])
    return kb(*rows)


def topic_picker_menu(topics: list, prefix: str, back: str) -> dict:
    rows = []
    for t in topics:
        icon = "📦 " if t["is_archive"] else ""
        rows.append([btn(f"{icon}{t['name']}", f"{prefix}:{t['id']}")])
    if not rows:
        rows.append([btn("(مبحثی وجود ندارد)", "noop")])
    rows.append([btn("🔙 بازگشت", back)])
    return kb(*rows)


def topic_detail_menu(topic: dict, section_key: str) -> dict:
    if topic["is_archive"]:
        return kb(
            [btn("❓ سؤالات این مبحث", f"menu:questions_list:{topic['id']}")],
            [btn("🔙 بازگشت", f"sec_topics:{section_key}")],
        )
    toggle_text = "🚫 غیرفعال کردن" if topic["is_active"] else "✅ فعال کردن"
    return kb(
        [btn("✏️ تغییر نام", f"topic:rename:{topic['id']}")],
        [btn(toggle_text, f"topic:toggle:{topic['id']}")],
        [btn("⬆️", f"topic:up:{topic['id']}"), btn("⬇️", f"topic:down:{topic['id']}")],
        [btn("❓ سؤالات این مبحث", f"menu:questions_list:{topic['id']}")],
        [btn("🗑 حذف مبحث", f"topic:delete:{topic['id']}")],
        [btn("🔙 بازگشت", f"sec_topics:{section_key}")],
    )


def questions_list_menu(questions: list, topic_id: int, is_archive: bool = False) -> dict:
    rows = []
    for i, q in enumerate(questions, start=1):
        short = (q["text"][:28] + "…") if len(q["text"]) > 28 else q["text"]
        rows.append([btn(f"{i}. {short}", f"q:open:{q['id']}")])
    if not is_archive:
        rows.append([btn("➕ افزودن سؤال جدید", f"q:add:{topic_id}")])
    rows.append([btn("🔙 بازگشت", "menu:questions_sections")])
    return kb(*rows)


def question_detail_menu(question_id: int, topic_id: int) -> dict:
    return kb(
        [btn("✏️ ویرایش متن سؤال", f"q:edit:{question_id}")],
        [btn("🗑 حذف سؤال", f"q:delete:{question_id}")],
        [btn("🔙 بازگشت", f"menu:questions_list:{topic_id}")],
    )


def correct_option_menu() -> dict:
    return kb([btn("A", "correct:A"), btn("B", "correct:B")], [btn("C", "correct:C"), btn("D", "correct:D")])


def quiz_setup_menu(selected_time: int | None, selected_count: int | None) -> dict:
    time_buttons = [
        btn(f"{'✅ ' if selected_time == t else ''}⏱ {t} ثانیه", f"qtime:{t}") for t in TIME_OPTIONS
    ]
    count_buttons = [
        btn(f"{'✅ ' if selected_count == c else ''}{c} سؤال", f"qcount:{c}") for c in COUNT_OPTIONS
    ]
    rows = [time_buttons[:2], time_buttons[2:]]
    rows += [count_buttons[:3], count_buttons[3:]]
    rows.append([btn("🚀 شروع کوییز", "qconfirm")])
    rows.append([btn("🔙 بازگشت", "menu:new_quiz")])
    return kb(*rows)


def join_keyboard(quiz_id: int, started: bool) -> dict:
    rows = [[btn("🎮 شرکت در کوییز", f"join:{quiz_id}")]]
    if not started:
        rows.append([btn("▶️ شروع کوییز", f"start:{quiz_id}")])
    return kb(*rows)


def question_options_keyboard(quiz_question_id: int, question: dict, closed: bool = False) -> dict:
    rows = []
    for opt in question["options"]:
        cb = "closed" if closed else f"ans:{quiz_question_id}:{opt['id']}"
        rows.append([btn(f"🔘 {opt['label']}) {opt['text']}", cb)])
    return kb(*rows)


def teacher_control_panel(quiz_id: int, paused: bool) -> dict:
    pause_btn = btn("▶️ ادامه", f"resume:{quiz_id}") if paused else btn("⏸ توقف موقت", f"pause:{quiz_id}")
    return kb([btn("⏭ سؤال بعدی", f"next:{quiz_id}"), pause_btn], [btn("🛑 لغو کوییز", f"cancel:{quiz_id}")])


def admins_menu() -> dict:
    rows = []
    for uid in db.list_admin_ids():
        if uid in ADMIN_IDS:
            rows.append([btn(f"🔒 {uid} (ثابت)", "noop")])
        else:
            rows.append([btn(f"👤 {uid}", "noop"), btn("❌ حذف", f"admin:remove:{uid}")])
    rows.append([btn("➕ افزودن ادمین جدید", "admin:add")])
    rows.append([btn("🔙 بازگشت", "menu:main")])
    return kb(*rows)


def stats_menu_keyboard() -> dict:
    return kb(
        [btn("🎛 مدیریت امتیازات", "menu:score_manage")],
        [btn("🔙 بازگشت", "menu:main")],
    )


def score_manage_list_menu() -> dict:
    users = db.overall_leaderboard(limit=100)
    rows = [
        [btn(f"{u['display_name']} — {fmt_score(u['total'])}", f"score:user:{u['user_id']}")]
        for u in users
    ]
    if not rows:
        rows.append([btn("(هنوز کسی امتیازی ندارد)", "noop")])
    rows.append([btn("🔙 بازگشت", "menu:stats")])
    return kb(*rows)


def score_user_detail_menu(user_id: int) -> dict:
    return kb(
        [btn("➕ افزودن امتیاز", f"score:add:{user_id}")],
        [btn("➖ کم کردن امتیاز", f"score:sub:{user_id}")],
        [btn("0️⃣ صفر کردن امتیاز", f"score:zero:{user_id}")],
        [btn("🗑 حذف کامل از رتبه‌بندی", f"score:remove:{user_id}")],
        [btn("🔙 بازگشت", "menu:score_manage")],
    )


def score_remove_confirm_menu(user_id: int) -> dict:
    return kb(
        [btn("✅ بله، کامل حذف کن", f"score:remove_confirm:{user_id}")],
        [btn("❌ نه، بی‌خیال", f"score:user:{user_id}")],
    )


def settings_menu() -> dict:
    show_correct = db.get_setting("show_correct_answer", "1") == "1"
    toggle_text = "✅ نمایش پاسخ درست بعد از سؤال" if show_correct else "🚫 نمایش پاسخ درست بعد از سؤال"
    return kb(
        [btn(toggle_text, "settings:toggle_show_correct")],
        [btn("🧮 تنظیم فرمول نمره‌دهی", "menu:score_formula")],
        [btn("🔙 بازگشت", "menu:main")],
    )


def score_formula_menu() -> dict:
    rows = [[btn(f"{c} سؤالی", f"formula:{c}")] for c in COUNT_OPTIONS]
    rows.append([btn("🔙 بازگشت", "menu:settings")])
    return kb(*rows)


BACK_MENU = kb([btn("🔙 بازگشت", "menu:main")])



# --------------------------------------------------------------------- #
# فرمت‌دهی متن‌ها
# --------------------------------------------------------------------- #

MEDALS = ["🥇", "🥈", "🥉"]


def medal_for_rank(i: int) -> str:
    if i < len(MEDALS):
        return MEDALS[i]
    return f"{i + 1}️⃣" if i < 9 else "🔹"


def fmt_score(x: float) -> str:
    x = round(float(x), 4)
    if x == int(x):
        return str(int(x))
    return f"{x:.2f}".rstrip("0").rstrip(".")


def format_participants_block(participants: list[dict]) -> str:
    if not participants:
        return "هنوز کسی ثبت‌نام نکرده ✨"
    lines = []
    for i, p in enumerate(participants):
        icon = medal_for_rank(i) if i < 2 else "🔸"
        lines.append(f"{icon} {p['display_name']}")
    return "\n".join(lines)


def format_leaderboard_block(participants: list[dict]) -> str:
    if not participants:
        return "هنوز امتیازی ثبت نشده."
    return "\n".join(
        f"{medal_for_rank(i)} {p['display_name']} — {fmt_score(p['total_score'])} امتیاز"
        for i, p in enumerate(participants)
    )


def render_question_text(question: dict, index: int, total: int, remaining: int) -> str:
    lines = [f"🔮 سؤال {index} از {total}", "", question["text"], ""]
    for opt in question["options"]:
        lines.append(f"🔘 {opt['label']}) {opt['text']}")
    lines.append("")
    if remaining > 0:
        lines.append(f"⏳ زمان باقی‌مانده: {remaining} ثانیه")
    else:
        lines.append("🔒 زمان پاسخ‌گویی به پایان رسید.")
    return "\n".join(lines)


def render_result_text(question: dict, answers: list[dict], participants: list[dict]) -> str:
    lines = []
    if db.get_setting("show_correct_answer", "1") == "1":
        correct = next((o for o in question["options"] if o["is_correct"]), None)
        if correct:
            lines.append(f"✅ پاسخ درست: گزینه {correct['label']}) {correct['text']}")
        else:
            lines.append("پاسخ این سؤال ثبت نشده بود.")
        lines.append("")
    if answers:
        lines.append("👥 پاسخ‌ها:")
        for a in answers:
            mark = "✅" if a["is_correct"] else "❌"
            lines.append(f"{a['display_name']} {mark} +{fmt_score(a['score'])}")
        lines.append("")
    lines.append("🏆 جدول امتیازات")
    lines.append(format_leaderboard_block(participants))
    return "\n".join(lines)


def render_announce_text(topic_name: str, participants: list[dict], time_s: int, count: int) -> str:
    return (
        "✨🏆 کوییز آماده‌ی شروعه! 🏆✨\n\n"
        f"📚 مبحث: {topic_name}\n"
        f"🔢 تعداد سؤال: {count}    ⏳ زمان هر سؤال: {time_s} ثانیه\n\n"
        "برای شرکت روی دکمه‌ی زیر بزن 👇\n\n"
        f"👥 شرکت‌کننده‌ها: {len(participants)} نفر\n"
        f"{format_participants_block(participants)}"
    )


# --------------------------------------------------------------------- #
# فرمول امتیازدهی
#
# جمع‌بندی: هر حالت طوری طراحی شده که اگر همه‌ی سؤالات درست جواب داده شوند
# و بدون تأخیر، مجموع به ۲۰ نمره برسد. دیر جواب دادن (نسبت به کل زمان
# مجاز آن سؤال) از همان نمره‌ی سؤال کم می‌کند. مقادیر پیش‌فرض:
#   ۱ سؤالی  -> هر سؤال ۲۰ نمره، جریمه‌ی تأخیر ۱ تا ۸ نمره (پیوسته با زمان)
#   ۵ سؤالی  -> هر سؤال ۴ نمره،  جریمه‌ی تأخیر ۰.۵ یا ۱ نمره
#   ۱۰ سؤالی -> هر سؤال ۲ نمره،  جریمه‌ی تأخیر ۰.۵ یا ۱ نمره
#   ۱۵ سؤالی -> هر سؤال ۲۰/۱۵ نمره، جریمه‌ی تأخیر ۰.۲۵ یا ۰.۵ نمره
#   ۲۰ سؤالی -> هر سؤال ۱ نمره،  جریمه‌ی تأخیر ۰.۲۵ یا ۰.۵ نمره
#
# این مقادیر (base/low/high) از داخل پنل («⚙️ تنظیمات» → «🧮 تنظیم فرمول
# نمره‌دهی») توسط ادمین قابل تغییرند - compute_score همیشه آخرین مقدار
# ذخیره‌شده در دیتابیس را می‌خواند، نه این اعداد پیش‌فرض را.
# --------------------------------------------------------------------- #

def compute_score(is_correct: bool, response_time: float, duration: int, question_count: int) -> float:
    if not is_correct:
        return 0.0

    ratio = 0.0 if duration <= 0 else min(1.0, max(0.0, response_time / duration))
    base, low, high = db.get_score_config(question_count)

    if question_count == 1:
        # سؤال تکی: جریمه‌ی پیوسته بین low و high بر اساس سرعت پاسخ.
        penalty = low + (high - low) * ratio
        penalty = min(high, max(low, round(penalty)))
        return round(max(0.0, base - penalty), 4)

    # بقیه‌ی حالت‌ها: جواب سریع (یک‌سوم اول زمان) هیچ جریمه‌ای ندارد؛ جواب
    # توی یک‌سومِ میانی جریمه‌ی low، و جواب توی یک‌سومِ آخر جریمه‌ی high.
    if ratio <= 1 / 3:
        penalty = 0.0
    elif ratio <= 2 / 3:
        penalty = low
    else:
        penalty = high

    return round(max(0.0, base - penalty), 4)


# --------------------------------------------------------------------- #
# موتور اجرای کوییز - هر کوییز در یک Thread جدا (به‌جای asyncio.Task)
# --------------------------------------------------------------------- #

class QuizControl:
    """کنترل زنده‌ی یک سؤال در حال اجرا: Pause/Resume/Skip/Cancel."""

    def __init__(self, total_seconds: int):
        self.total_seconds = total_seconds
        self.lock = threading.RLock()
        self.start_ts = time.monotonic()
        self.elapsed_before_pause = 0.0
        self.paused = False
        self.paused_at: float | None = None
        self.cancelled = False
        self.skip_flag = False

    def pause(self) -> None:
        with self.lock:
            if not self.paused:
                self.paused = True
                self.paused_at = time.monotonic()

    def resume(self) -> None:
        with self.lock:
            if self.paused and self.paused_at is not None:
                self.elapsed_before_pause += time.monotonic() - self.paused_at
                self.paused = False
                self.paused_at = None

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True

    def skip(self) -> None:
        with self.lock:
            self.skip_flag = True

    def elapsed(self) -> float:
        with self.lock:
            if self.paused and self.paused_at is not None:
                return self.paused_at - self.start_ts - self.elapsed_before_pause
            return time.monotonic() - self.start_ts - self.elapsed_before_pause

    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed())


_active_lock = threading.RLock()
_controls: dict[int, QuizControl] = {}
_threads: dict[int, threading.Thread] = {}


def is_running(quiz_id: int) -> bool:
    with _active_lock:
        return quiz_id in _threads


def is_paused(quiz_id: int) -> bool:
    with _active_lock:
        c = _controls.get(quiz_id)
    return bool(c and c.paused)


def pause_quiz(quiz_id: int) -> bool:
    with _active_lock:
        c = _controls.get(quiz_id)
    if c:
        c.pause()
        return True
    return False


def resume_quiz(quiz_id: int) -> bool:
    with _active_lock:
        c = _controls.get(quiz_id)
    if c:
        c.resume()
        return True
    return False


def skip_current(quiz_id: int) -> None:
    with _active_lock:
        c = _controls.get(quiz_id)
    if c:
        c.skip()


def cancel_quiz(quiz_id: int) -> None:
    with _active_lock:
        c = _controls.get(quiz_id)
    if c:
        c.cancel()
    db.set_status(quiz_id, "cancelled")


def _select_question_ids(quiz: dict) -> list[int]:
    questions = db.list_questions(quiz["topic_id"])
    random.shuffle(questions)
    count = quiz["question_count"] or len(questions)
    return [q["id"] for q in questions[:count]]


def start_quiz(quiz_id: int) -> None:
    with _active_lock:
        if quiz_id in _threads:
            return
    quiz = db.get_quiz(quiz_id)
    if not quiz:
        return
    question_ids = _select_question_ids(quiz)
    if not question_ids:
        return
    db.build_quiz_questions(quiz_id, question_ids, quiz["question_time_seconds"])
    db.set_status(quiz_id, "running")
    t = threading.Thread(target=_run_quiz, args=(quiz_id, 0), daemon=True)
    with _active_lock:
        _threads[quiz_id] = t
    t.start()


def _run_quiz(quiz_id: int, start_order: int) -> None:
    try:
        order = start_order
        while True:
            outcome = _run_single_question(quiz_id, order)
            if outcome == "no_more":
                _finish_quiz(quiz_id)
                break
            if outcome == "cancelled":
                _announce_cancelled(quiz_id)
                break
            order += 1
            time.sleep(PAUSE_BETWEEN_QUESTIONS)
    except Exception:
        log.exception("quiz thread crashed for quiz %s", quiz_id)
    finally:
        with _active_lock:
            _threads.pop(quiz_id, None)
            _controls.pop(quiz_id, None)


def _shuffle_options_for_display(question: dict) -> dict:
    """هر بار که یک سؤال پرسیده می‌شود، جای چهار گزینه (فقط جایشان، نه
    محتوایشان) رندوم عوض می‌شود - این‌طوری دانش‌آموزها نمی‌توانند جای دکمه‌ی
    جواب را (مثلاً همیشه گزینه‌ی دوم) از قبل حفظ کنند. این کار روی یک کپی
    انجام می‌شود، نه روی دیتابیس - پس بانک سؤالات دست‌نخورده می‌ماند."""
    options = [dict(o) for o in question["options"]]
    random.shuffle(options)
    for i, opt in enumerate(options):
        opt["label"] = OPTION_LABELS[i] if i < len(OPTION_LABELS) else str(i + 1)
    shuffled = dict(question)
    shuffled["options"] = options
    return shuffled


def _run_single_question(quiz_id: int, order: int) -> str:
    qq = db.get_quiz_question_by_order(quiz_id, order)
    if qq is None:
        return "no_more"
    quiz = db.get_quiz(quiz_id)
    if quiz is None or quiz["status"] == "cancelled":
        return "cancelled"

    total = len(quiz["quiz_questions"])
    # این‌جا فقط برای نمایش (متن سؤال و دکمه‌ها) از نسخه‌ی شافل‌شده استفاده
    # می‌شود؛ callback_data دکمه‌ها هنوز روی همان option id واقعی است، پس
    # امتیازدهی و تشخیص «کدام گزینه درست است» کاملاً درست کار می‌کند.
    question = _shuffle_options_for_display(qq["question"])
    duration = qq["duration_seconds"]
    keyboard = question_options_keyboard(qq["id"], question, closed=False)
    msg = send_message(quiz["chat_id"], render_question_text(question, order + 1, total, duration), keyboard)
    if not msg:
        return "cancelled"
    db.mark_question_started(qq["id"], msg["message_id"])
    db.set_status(quiz_id, "question_active")

    control = QuizControl(duration)
    with _active_lock:
        _controls[quiz_id] = control

    last_rendered = -1
    while True:
        if control.cancelled or control.skip_flag:
            break
        if control.paused:
            time.sleep(0.5)
            continue
        remaining = control.remaining()
        if remaining <= 0:
            break
        rounded = int(remaining)
        if rounded != last_rendered:
            edit_message_text(
                quiz["chat_id"], msg["message_id"],
                render_question_text(question, order + 1, total, rounded), keyboard,
            )
            last_rendered = rounded
        time.sleep(min(TIMER_UPDATE_INTERVAL, max(0.5, remaining)))

    cancelled = control.cancelled
    if not cancelled:
        closed_kb = question_options_keyboard(qq["id"], question, closed=True)
        edit_message_text(
            quiz["chat_id"], msg["message_id"],
            render_question_text(question, order + 1, total, 0), closed_kb,
        )

    with _active_lock:
        _controls.pop(quiz_id, None)

    if cancelled:
        return "cancelled"

    db.mark_question_finished(qq["id"])
    db.set_status(quiz_id, "question_finished")
    answers = db.answers_for_question(qq["id"])
    participants = db.list_participants(quiz_id)
    send_message(quiz["chat_id"], render_result_text(question, answers, participants))
    return "ok"


def _finish_quiz(quiz_id: int) -> None:
    quiz = db.get_quiz(quiz_id)
    if not quiz:
        return
    db.set_status(quiz_id, "finished")
    participants = db.list_participants(quiz_id)
    topic_name = quiz["topic_name"] or "—"
    lines = ["🎉✨ کوییز تموم شد! ✨🎉", "", f"📚 مبحث: {topic_name}", "", "🏅 نتیجه‌ی نهایی:"]
    lines.append(format_leaderboard_block(participants))
    if participants:
        lines.append("")
        lines.append(f"🌟 نفر برتر: {participants[0]['display_name']}")
    send_message(quiz["chat_id"], "\n".join(lines))


def _announce_cancelled(quiz_id: int) -> None:
    quiz = db.get_quiz(quiz_id)
    if quiz:
        send_message(quiz["chat_id"], "🛑 کوییز توسط ادمین لغو شد.")


def recover_all() -> None:
    """در startup صدا زده می‌شود - هر کوییزی که هنگام قطع‌شدن ربات زنده بوده را
    به‌طور امن ادامه می‌دهد (نگاه کنید به README برای توضیح این رفتار)."""
    for quiz_id in db.list_recoverable_quizzes():
        try:
            _recover_one(quiz_id)
        except Exception:
            log.exception("recovery failed for quiz %s", quiz_id)


def _recover_one(quiz_id: int) -> None:
    quiz = db.get_quiz(quiz_id)
    if not quiz:
        return
    unfinished = [q for q in quiz["quiz_questions"] if not q["is_finished"]]
    if not unfinished:
        _finish_quiz(quiz_id)
        return

    current = min(unfinished, key=lambda q: q["order_index"])
    resume_order = current["order_index"]

    if current["started_at"] is not None:
        question = db.get_question(current["question_id"])
        if current["message_id"]:
            try:
                edit_message_text(
                    quiz["chat_id"], current["message_id"],
                    render_question_text(question, current["order_index"] + 1, len(quiz["quiz_questions"]), 0),
                    question_options_keyboard(current["id"], question, closed=True),
                )
            except Exception:
                pass
        db.mark_question_finished(current["id"])
        answers = db.answers_for_question(current["id"])
        participants = db.list_participants(quiz_id)
        result_text = render_result_text(question, answers, participants)
        send_message(
            quiz["chat_id"],
            "♻️ ربات دوباره روشن شد، خیالت راحت هیچی از دست نرفت. کوییز همین‌جوری ادامه پیدا می‌کنه ✨\n\n" + result_text,
        )
        resume_order = current["order_index"] + 1

    db.set_status(quiz_id, "running")
    t = threading.Thread(target=_run_quiz, args=(quiz_id, resume_order), daemon=True)
    with _active_lock:
        _threads[quiz_id] = t
    t.start()


# --------------------------------------------------------------------- #
# Handlerهای پیام (Command / مراحل FSM)
# --------------------------------------------------------------------- #

START_TEXT = (
    "🔮✨ درود الهوریسی! ✨🔮\n"
    "‌\n"
    "من ربات کوییز الهوریسم 🧙‍♂️ و ازتون سوال میپرسم، امیدوارم خوب درساتو خونده باشی 📖💫\n\n"
    "برای شرکت و جواب دادن سوال‌ها باید منتظر پیام شروع باشی که توسط ادمین‌ها یا استاد ارسال میشه 🎮\n\n"
    "🪄 برای ورود به پنل مدیریت از /admin استفاده کن."
)


def _command_name(text: str) -> str | None:
    """تشخیص دستور، حتی وقتی تلگرام نام ربات را به آن می‌چسباند
    (مثلاً '/admin@YourBotName' به‌جای '/admin' - این توی گروه‌ها معمول است)."""
    if not text.startswith("/"):
        return None
    first_token = text.split()[0]
    return first_token.split("@")[0]


def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user = msg.get("from")
    if not user or user.get("is_bot"):
        return
    user_id = user["id"]
    db.get_or_create_user(user_id, display_name_of(user), user.get("username"))
    text = (msg.get("text") or "").strip()
    command = _command_name(text)

    if command == "/start":
        send_message(chat_id, START_TEXT)
        return

    if command == "/admin":
        clear_state(chat_id, user_id)
        if not is_admin(user_id):
            # کاربران عادی هیچ اطلاعاتی از مباحث/سؤالات نمی‌بینند و به هیچ
            # بخش مدیریتی دسترسی ندارند - فقط همین پیام را می‌بینند.
            send_message(chat_id, "⛔️ این بخش فقط برای ادمین‌ها و استاده.")
            return
        send_message(chat_id, "🪄 پنل مدیریت", main_admin_menu())
        return

    state = get_state(chat_id, user_id)
    if state:
        _handle_fsm_message(chat_id, user_id, state, text)


def _handle_fsm_message(chat_id: int, user_id: int, state: dict, text: str) -> None:
    name = state["name"]

    if name == "add_topic":
        if not text:
            send_message(chat_id, "❌ نام مبحث نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return
        section = db.get_section_by_key(state["section_key"])
        db.add_topic(section["id"], text)
        topics = db.list_topics(section["id"])
        clear_state(chat_id, user_id)
        send_message(chat_id, f"✅ مبحث «{text}» اضافه شد.", topics_management_menu(topics, state["section_key"]))

    elif name == "rename_topic":
        if not text:
            send_message(chat_id, "❌ نام مبحث نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return
        db.rename_topic(state["topic_id"], text)
        topic = db.get_topic(state["topic_id"])
        section = db.get_section(topic["section_id"])
        clear_state(chat_id, user_id)
        send_message(chat_id, f"✅ نام مبحث به «{text}» تغییر یافت.", topic_detail_menu(topic, section["key"]))

    elif name == "edit_question_text":
        db.update_question_text(state["question_id"], text)
        question = db.get_question(state["question_id"])
        clear_state(chat_id, user_id)
        options_text = "\n".join(f"{o['label']}) {o['text']}{' ✅' if o['is_correct'] else ''}" for o in question["options"])
        send_message(
            chat_id, f"✅ متن سؤال به‌روزرسانی شد.\n\n❓ {question['text']}\n\n{options_text}",
            question_detail_menu(question["id"], question["topic_id"]),
        )

    elif name == "add_question_text":
        if not text:
            send_message(chat_id, "❌ متن سؤال نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return
        set_state(chat_id, user_id, "add_question_opt_a", topic_id=state["topic_id"], question_text=text)
        send_message(chat_id, "گزینه A را ارسال کنید:")

    elif name == "add_question_opt_a":
        set_state(chat_id, user_id, "add_question_opt_b", **{**state_data(state), "opt_a": text})
        send_message(chat_id, "گزینه B را ارسال کنید:")

    elif name == "add_question_opt_b":
        set_state(chat_id, user_id, "add_question_opt_c", **{**state_data(state), "opt_b": text})
        send_message(chat_id, "گزینه C را ارسال کنید:")

    elif name == "add_question_opt_c":
        set_state(chat_id, user_id, "add_question_opt_d", **{**state_data(state), "opt_c": text})
        send_message(chat_id, "گزینه D را ارسال کنید:")

    elif name == "add_question_opt_d":
        set_state(chat_id, user_id, "add_question_correct", **{**state_data(state), "opt_d": text})
        send_message(chat_id, "✅ گزینه صحیح کدام است؟", correct_option_menu())

    elif name == "add_admin_id":
        cleaned = text.strip().lstrip("@")
        if not cleaned.isdigit():
            send_message(chat_id, "❌ لطفاً فقط آیدی عددی تلگرام را ارسال کنید (مثال: 123456789).")
            return
        new_admin_id = int(cleaned)
        added = db.add_admin(new_admin_id)
        clear_state(chat_id, user_id)
        msg = f"✅ کاربر {new_admin_id} به ادمین‌ها اضافه شد." if added else "ℹ️ این کاربر از قبل ادمین بود."
        send_message(chat_id, msg, admins_menu())

    elif name in ("score_add_amount", "score_sub_amount"):
        try:
            amount = abs(float(text.strip().replace(",", ".")))
        except ValueError:
            send_message(chat_id, "❌ لطفاً فقط یک عدد بفرست (مثلاً 5 یا 2.5):")
            return
        target_id = state["target_id"]
        delta = amount if name == "score_add_amount" else -amount
        db.add_score_adjustment(target_id, delta)
        clear_state(chat_id, user_id)
        u = db.get_user(target_id)
        uname = u["display_name"] if u else str(target_id)
        total = db.get_user_total_score(target_id)
        verb = "اضافه شد" if delta > 0 else "کم شد"
        send_message(
            chat_id, f"✅ {fmt_score(amount)} امتیاز {verb}.\n\n👤 {uname}\nامتیاز فعلی: {fmt_score(total)}",
            score_user_detail_menu(target_id),
        )

    elif name == "edit_formula":
        parts = text.strip().replace(",", " ").split()
        if len(parts) != 3:
            send_message(chat_id, "❌ باید دقیقاً ۳ عدد با فاصله بفرستی: نمره_پایه جریمه_کوچک جریمه_بزرگ")
            return
        try:
            base, low, high = (float(p) for p in parts)
        except ValueError:
            send_message(chat_id, "❌ هر سه مقدار باید عدد باشند. دوباره امتحان کن:")
            return
        if low > high:
            low, high = high, low
        count = state["count"]
        db.set_score_config(count, base, low, high)
        clear_state(chat_id, user_id)
        send_message(
            chat_id,
            f"✅ فرمول {count} سؤالی به‌روزرسانی شد: پایه={fmt_score(base)}، جریمه‌ی کوچک={fmt_score(low)}، جریمه‌ی بزرگ={fmt_score(high)}",
            score_formula_menu(),
        )


# --------------------------------------------------------------------- #
# Handlerهای Callback (دکمه‌ها)
# --------------------------------------------------------------------- #

def handle_callback(cq: dict) -> None:
    data = cq.get("data", "")
    user = cq["from"]
    user_id = user["id"]
    message = cq.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    cq_id = cq["id"]

    if chat_id is None:
        answer_callback_query(cq_id)
        return

    db.get_or_create_user(user_id, display_name_of(user), user.get("username"))

    def require_admin() -> bool:
        if not is_admin(user_id):
            answer_callback_query(cq_id, "⛔️ این بخش فقط برای ادمین‌ها و استاده.", show_alert=True)
            return False
        return True

    try:
        _dispatch_callback(data, chat_id, user_id, message_id, cq_id, require_admin)
    except Exception:
        log.exception("Error handling callback %s", data)
        answer_callback_query(cq_id, "⚠️ خطایی رخ داد.", show_alert=True)


def _dispatch_callback(data, chat_id, user_id, message_id, cq_id, require_admin) -> None:
    # ---------------- ناوبری اصلی پنل ---------------- #
    if data == "noop":
        answer_callback_query(cq_id)

    elif data == "menu:main":
        clear_state(chat_id, user_id)
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "🪄 پنل مدیریت", main_admin_menu())
        answer_callback_query(cq_id)

    elif data == "menu:topics_sections":
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "کدام بخش را می‌خواهید مدیریت کنید؟", sections_menu("sec_topics"))
        answer_callback_query(cq_id)

    elif data == "menu:questions_sections":
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "سؤالات کدام بخش را می‌خواهید مدیریت کنید؟", sections_menu("sec_questions"))
        answer_callback_query(cq_id)

    # ---------------- مدیریت مباحث ---------------- #
    elif data.startswith("sec_topics:"):
        if not require_admin():
            return
        section_key = data.split(":", 1)[1]
        section = db.get_section_by_key(section_key)
        topics = db.list_topics(section["id"])
        edit_message_text(
            chat_id, message_id, f"{section['title']}\n\nمباحث موجود:",
            topics_management_menu(topics, section_key),
        )
        answer_callback_query(cq_id)

    elif data.startswith("topic:open:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[2])
        topic = db.get_topic(topic_id)
        section = db.get_section(topic["section_id"])
        note = "\n📦 این مبحث خودکار آرشیو می‌شود و قابل تغییر نام/حذف نیست." if topic["is_archive"] else ""
        edit_message_text(
            chat_id, message_id,
            f"📚 مبحث: {topic['name']}\nوضعیت: {'فعال ✅' if topic['is_active'] else 'غیرفعال 🚫'}{note}",
            topic_detail_menu(topic, section["key"]),
        )
        answer_callback_query(cq_id)

    elif data.startswith("topic:add:"):
        if not require_admin():
            return
        section_key = data.split(":", 2)[2]
        set_state(chat_id, user_id, "add_topic", section_key=section_key)
        edit_message_text(chat_id, message_id, "✏️ نام مبحث جدید را ارسال کنید:")
        answer_callback_query(cq_id)

    elif data.startswith("topic:rename:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[2])
        set_state(chat_id, user_id, "rename_topic", topic_id=topic_id)
        edit_message_text(chat_id, message_id, "✏️ نام جدید مبحث را ارسال کنید:")
        answer_callback_query(cq_id)

    elif data.startswith("topic:toggle:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[2])
        topic = db.get_topic(topic_id)
        db.set_topic_active(topic_id, not topic["is_active"])
        topic = db.get_topic(topic_id)
        section = db.get_section(topic["section_id"])
        edit_message_text(
            chat_id, message_id,
            f"📚 مبحث: {topic['name']}\nوضعیت: {'فعال ✅' if topic['is_active'] else 'غیرفعال 🚫'}",
            topic_detail_menu(topic, section["key"]),
        )
        answer_callback_query(cq_id, "✅ به‌روزرسانی شد.")

    elif data.startswith("topic:up:") or data.startswith("topic:down:"):
        if not require_admin():
            return
        parts = data.split(":")
        direction = -1 if parts[1] == "up" else 1
        topic_id = int(parts[2])
        db.move_topic(topic_id, direction)
        topic = db.get_topic(topic_id)
        section = db.get_section(topic["section_id"])
        edit_message_text(
            chat_id, message_id,
            f"📚 مبحث: {topic['name']}\nوضعیت: {'فعال ✅' if topic['is_active'] else 'غیرفعال 🚫'}",
            topic_detail_menu(topic, section["key"]),
        )
        answer_callback_query(cq_id)

    elif data.startswith("topic:delete:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[2])
        topic = db.get_topic(topic_id)
        section = db.get_section(topic["section_id"])
        deleted = db.delete_topic(topic_id)
        if not deleted:
            answer_callback_query(cq_id, "⛔️ این مبحث آرشیوی است و قابل حذف نیست.", show_alert=True)
            return
        topics = db.list_topics(section["id"])
        edit_message_text(
            chat_id, message_id, f"🗑 مبحث حذف شد.\n\n{section['title']}\n\nمباحث موجود:",
            topics_management_menu(topics, section["key"]),
        )
        answer_callback_query(cq_id)

    # ---------------- مدیریت سوالات ---------------- #
    elif data.startswith("sec_questions:"):
        if not require_admin():
            return
        section_key = data.split(":", 1)[1]
        section = db.get_section_by_key(section_key)
        topics = db.list_topics(section["id"])
        edit_message_text(
            chat_id, message_id, "مبحث موردنظر برای مدیریت سؤالات را انتخاب کنید:",
            topic_picker_menu(topics, "menu:questions_list", "menu:questions_sections"),
        )
        answer_callback_query(cq_id)

    elif data.startswith("menu:questions_list:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[2])
        topic = db.get_topic(topic_id)
        questions = db.list_questions(topic_id)
        header = f"📦 {topic['name']} (آرشیو خودکار)" if topic["is_archive"] else f"📚 {topic['name']}"
        edit_message_text(
            chat_id, message_id, f"{header}\n\nسؤالات این مبحث:",
            questions_list_menu(questions, topic_id, is_archive=bool(topic["is_archive"])),
        )
        answer_callback_query(cq_id)

    elif data.startswith("q:open:"):
        if not require_admin():
            return
        question_id = int(data.split(":")[2])
        question = db.get_question(question_id)
        options_text = "\n".join(f"{o['label']}) {o['text']}{' ✅' if o['is_correct'] else ''}" for o in question["options"])
        edit_message_text(
            chat_id, message_id, f"❓ {question['text']}\n\n{options_text}",
            question_detail_menu(question_id, question["topic_id"]),
        )
        answer_callback_query(cq_id)

    elif data.startswith("q:delete:"):
        if not require_admin():
            return
        question_id = int(data.split(":")[2])
        question = db.get_question(question_id)
        topic_id = question["topic_id"]
        db.delete_question(question_id)
        topic = db.get_topic(topic_id)
        questions = db.list_questions(topic_id)
        header = f"📦 {topic['name']} (آرشیو خودکار)" if topic["is_archive"] else f"📚 {topic['name']}"
        edit_message_text(
            chat_id, message_id, f"🗑 سؤال حذف شد.\n\n{header}\n\nسؤالات این مبحث:",
            questions_list_menu(questions, topic_id, is_archive=bool(topic["is_archive"])),
        )
        answer_callback_query(cq_id)

    elif data.startswith("q:edit:"):
        if not require_admin():
            return
        question_id = int(data.split(":")[2])
        set_state(chat_id, user_id, "edit_question_text", question_id=question_id)
        edit_message_text(chat_id, message_id, "✏️ متن جدید سؤال را ارسال کنید:")
        answer_callback_query(cq_id)

    elif data.startswith("q:add:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[2])
        topic = db.get_topic(topic_id)
        if topic and topic["is_archive"]:
            answer_callback_query(cq_id, "📦 این مبحث فقط آرشیو خودکار است؛ سؤال را از مبحث اصلی‌اش اضافه کنید.", show_alert=True)
            return
        set_state(chat_id, user_id, "add_question_text", topic_id=topic_id)
        edit_message_text(chat_id, message_id, "✏️ متن سؤال را ارسال کنید:")
        answer_callback_query(cq_id)

    elif data.startswith("correct:"):
        if not require_admin():
            return
        state = pop_state_if(chat_id, user_id, "add_question_correct")
        if not state:
            # یا حالت دیگری فعال است، یا این همان دکمه‌ای است که یک کلیک
            # سریع‌تر قبلاً پردازشش کرده - این‌طوری سؤال دوبار ذخیره نمی‌شود.
            answer_callback_query(cq_id)
            return
        correct_label = data.split(":")[1]
        options = [("A", state["opt_a"]), ("B", state["opt_b"]), ("C", state["opt_c"]), ("D", state["opt_d"])]
        db.add_question(state["topic_id"], state["question_text"], options, correct_label)
        topic = db.get_topic(state["topic_id"])
        questions = db.list_questions(state["topic_id"])
        edit_message_text(
            chat_id, message_id,
            f"✅ سؤال ذخیره شد و توی آرشیوِ کلی هم کپی شد 📦✨\n\n📚 {topic['name']}\n\nسؤالات این مبحث:",
            questions_list_menu(questions, topic["id"], is_archive=bool(topic["is_archive"])),
        )
        answer_callback_query(cq_id)

    # ---------------- برگزاری کوییز ---------------- #
    elif data == "menu:new_quiz":
        if not require_admin():
            return
        if db.get_active_quiz_for_chat(chat_id):
            answer_callback_query(cq_id, "⚠️ یک کوییز فعال دیگر همین الان در این گروه در حال اجراست.", show_alert=True)
            return
        clear_state(chat_id, user_id)
        edit_message_text(chat_id, message_id, "بخش موردنظر برای کوییز را انتخاب کنید:", sections_menu("quiz_sec"))
        answer_callback_query(cq_id)

    elif data.startswith("quiz_sec:"):
        if not require_admin():
            return
        section_key = data.split(":", 1)[1]
        section = db.get_section_by_key(section_key)
        topics = db.list_topics(section["id"], active_only=True)
        edit_message_text(chat_id, message_id, "مبحث کوییز را انتخاب کنید:", topic_picker_menu(topics, "quiz_topic", "menu:new_quiz"))
        answer_callback_query(cq_id)

    elif data.startswith("quiz_topic:"):
        if not require_admin():
            return
        topic_id = int(data.split(":")[1])
        if db.count_questions(topic_id) == 0:
            answer_callback_query(cq_id, "⚠️ این مبحث هنوز سؤالی ندارد.", show_alert=True)
            return
        set_state(chat_id, user_id, "quiz_setup", topic_id=topic_id, time=None, count=None)
        edit_message_text(chat_id, message_id, "⏳ زمان هر سؤال و تعداد سؤالات را انتخاب کن:", quiz_setup_menu(None, None))
        answer_callback_query(cq_id)

    elif data.startswith("qtime:"):
        if not require_admin():
            return
        state = get_state(chat_id, user_id)
        if not state or state.get("name") != "quiz_setup":
            answer_callback_query(cq_id)
            return
        seconds = int(data.split(":")[1])
        update_state(chat_id, user_id, time=seconds)
        edit_message_reply_markup(chat_id, message_id, quiz_setup_menu(seconds, state.get("count")))
        answer_callback_query(cq_id)

    elif data.startswith("qcount:"):
        if not require_admin():
            return
        state = get_state(chat_id, user_id)
        if not state or state.get("name") != "quiz_setup":
            answer_callback_query(cq_id)
            return
        count = int(data.split(":")[1])
        update_state(chat_id, user_id, count=count)
        edit_message_reply_markup(chat_id, message_id, quiz_setup_menu(state.get("time"), count))
        answer_callback_query(cq_id)

    elif data == "qconfirm":
        if not require_admin():
            return
        state = get_state(chat_id, user_id)
        if not state or state.get("name") != "quiz_setup":
            answer_callback_query(cq_id)
            return
        seconds, count, topic_id = state.get("time"), state.get("count"), state.get("topic_id")
        if seconds is None or count is None:
            answer_callback_query(cq_id, "⚠️ لطفاً هم زمان و هم تعداد سؤال را انتخاب کن.", show_alert=True)
            return
        if db.get_active_quiz_for_chat(chat_id):
            answer_callback_query(cq_id, "⚠️ یک کوییز فعال دیگر همین الان در این گروه در حال اجراست.", show_alert=True)
            clear_state(chat_id, user_id)
            return
        topic = db.get_topic(topic_id)
        available = db.count_questions(topic_id)
        if available == 0:
            answer_callback_query(cq_id, "⚠️ این مبحث دیگر سؤالی ندارد.", show_alert=True)
            clear_state(chat_id, user_id)
            return
        quiz_id = db.create_quiz(chat_id, user_id, topic_id, seconds, count)
        clear_state(chat_id, user_id)
        text = render_announce_text(topic["name"], [], seconds, count)
        if available < count:
            text += f"\n\n⚠️ توجه: فقط {available} سؤال در این مبحث موجود است؛ به‌جای {count} سؤال، همان {available} سؤال پرسیده می‌شود (نمره‌دهی طبق حالت {count} سؤالی محاسبه می‌شود)."
        delete_message(chat_id, message_id)
        sent = send_message(chat_id, text, join_keyboard(quiz_id, started=False))
        if sent:
            db.set_announce_message(quiz_id, sent["message_id"])
        answer_callback_query(cq_id)

    # ---------------- ثبت‌نام / شروع ---------------- #
    elif data.startswith("join:"):
        quiz_id = int(data.split(":")[1])
        quiz = db.get_quiz(quiz_id)
        if quiz is None:
            answer_callback_query(cq_id, "❌ این کوییز دیگر وجود ندارد.", show_alert=True)
            return
        if quiz["status"] != "waiting_for_players":
            answer_callback_query(cq_id, "❌ ثبت‌نام برای این کوییز بسته شده است.", show_alert=True)
            return
        created = db.add_participant(quiz_id, user_id)
        if not created:
            answer_callback_query(cq_id, "ℹ️ شما قبلاً ثبت‌نام کرده‌اید.", show_alert=True)
            return
        participants = db.list_participants(quiz_id)
        topic_name = quiz["topic_name"] or "—"
        edit_message_text(
            chat_id, message_id,
            render_announce_text(topic_name, participants, quiz["question_time_seconds"], quiz["question_count"]),
            join_keyboard(quiz_id, started=False),
        )
        answer_callback_query(cq_id, "✅ ثبت‌نام شدی! خوش اومدی ✨")

    elif data.startswith("start:"):
        if not require_admin():
            return
        quiz_id = int(data.split(":")[1])
        quiz = db.get_quiz(quiz_id)
        if quiz is None or quiz["status"] != "waiting_for_players":
            answer_callback_query(cq_id, "❌ این کوییز قابل شروع نیست.", show_alert=True)
            return
        participants = db.list_participants(quiz_id)
        if not participants:
            answer_callback_query(cq_id, "⚠️ حداقل یک شرکت‌کننده لازم است.", show_alert=True)
            return
        topic_name = quiz["topic_name"] or "—"
        edit_message_text(
            chat_id, message_id,
            f"🚀✨ کوییز شروع شد! ✨🚀\n\n📚 مبحث: {topic_name}\n👥 شرکت‌کننده‌ها: {len(participants)} نفر\n\nسؤال اول به‌زودی می‌رسه...",
            teacher_control_panel(quiz_id, paused=False),
        )
        start_quiz(quiz_id)
        answer_callback_query(cq_id, "▶️ کوییز شروع شد.")

    # ---------------- پنل کنترل زنده ---------------- #
    elif data.startswith("next:"):
        if not require_admin():
            return
        quiz_id = int(data.split(":")[1])
        if not is_running(quiz_id):
            answer_callback_query(cq_id, "⚠️ در حال حاضر کوییزی در حال اجرا نیست.", show_alert=True)
            return
        skip_current(quiz_id)
        answer_callback_query(cq_id, "⏭ رفتیم سؤال بعدی...")

    elif data.startswith("pause:"):
        if not require_admin():
            return
        quiz_id = int(data.split(":")[1])
        if not pause_quiz(quiz_id):
            answer_callback_query(cq_id, "⚠️ در حال حاضر سؤال فعالی وجود ندارد.", show_alert=True)
            return
        edit_message_reply_markup(chat_id, message_id, teacher_control_panel(quiz_id, paused=True))
        answer_callback_query(cq_id, "⏸ کوییز موقتاً متوقف شد.")

    elif data.startswith("resume:"):
        if not require_admin():
            return
        quiz_id = int(data.split(":")[1])
        if not resume_quiz(quiz_id):
            answer_callback_query(cq_id, "⚠️ در حال حاضر سؤال فعالی وجود ندارد.", show_alert=True)
            return
        edit_message_reply_markup(chat_id, message_id, teacher_control_panel(quiz_id, paused=False))
        answer_callback_query(cq_id, "▶️ کوییز ادامه یافت.")

    elif data.startswith("cancel:"):
        if not require_admin():
            return
        quiz_id = int(data.split(":")[1])
        cancel_quiz(quiz_id)
        answer_callback_query(cq_id, "🛑 کوییز لغو شد.")

    # ---------------- پاسخ‌دهی ---------------- #
    elif data == "closed":
        answer_callback_query(cq_id, "🔒 زمان پاسخ‌گویی به این سؤال تمام شده است.", show_alert=True)

    elif data.startswith("ans:"):
        _handle_answer(data, chat_id, user_id, cq_id)

    # ---------------- آمار ---------------- #
    elif data == "menu:participants":
        if not require_admin():
            return
        active = db.get_active_quiz_for_chat(chat_id)
        if not active:
            edit_message_text(chat_id, message_id, "👥 در حال حاضر کوییز فعالی در این گروه وجود ندارد.", BACK_MENU)
            answer_callback_query(cq_id)
            return
        participants = db.list_participants(active["id"])
        topic_name = active["topic_name"] or "—"
        text = f"👥 شرکت‌کننده‌های کوییز جاری\n📚 مبحث: {topic_name}\n\nتعداد: {len(participants)} نفر\n\n{format_participants_block(participants)}"
        edit_message_text(chat_id, message_id, text, BACK_MENU)
        answer_callback_query(cq_id)

    elif data == "menu:stats":
        if not require_admin():
            return
        leaderboard = db.overall_leaderboard()
        if not leaderboard:
            text = "📊 هنوز امتیازی ثبت نشده است."
        else:
            lines = ["🏆 رتبه‌بندی کلی", ""]
            for i, row in enumerate(leaderboard):
                lines.append(f"{medal_for_rank(i)} {row['display_name']} — {fmt_score(row['total'])}")
            text = "\n".join(lines)
        edit_message_text(chat_id, message_id, text, stats_menu_keyboard())
        answer_callback_query(cq_id)

    elif data == "menu:history":
        if not require_admin():
            return
        quizzes = db.list_finished_quizzes(chat_id)
        if not quizzes:
            text = "🗂 هنوز کوییزی در این گروه به پایان نرسیده است."
        else:
            lines = ["🗂 سوابق کوییزهای این گروه", ""]
            for q in quizzes:
                finished = q["finished_at"][:16].replace("T", " ") if q["finished_at"] else "-"
                lines.append(f"• {q['topic_name']} — {finished}")
            text = "\n".join(lines)
        edit_message_text(chat_id, message_id, text, BACK_MENU)
        answer_callback_query(cq_id)

    # ---------------- مدیریت امتیازات (دستی) ---------------- #
    elif data == "menu:score_manage":
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "🎛 مدیریت امتیازات\n\nیک نفر را انتخاب کن:", score_manage_list_menu())
        answer_callback_query(cq_id)

    elif data.startswith("score:user:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        total = db.get_user_total_score(target_id)
        u = db.get_user(target_id)
        name = u["display_name"] if u else str(target_id)
        edit_message_text(
            chat_id, message_id, f"👤 {name}\nامتیاز فعلی: {fmt_score(total)}\n\nچی‌کار کنم؟",
            score_user_detail_menu(target_id),
        )
        answer_callback_query(cq_id)

    elif data.startswith("score:add:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        set_state(chat_id, user_id, "score_add_amount", target_id=target_id)
        edit_message_text(chat_id, message_id, "🔢 چند امتیاز اضافه بشه؟ یک عدد بفرست (مثلاً 5 یا 2.5):")
        answer_callback_query(cq_id)

    elif data.startswith("score:sub:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        set_state(chat_id, user_id, "score_sub_amount", target_id=target_id)
        edit_message_text(chat_id, message_id, "🔢 چند امتیاز کم بشه؟ یک عدد بفرست (مثلاً 5 یا 2.5):")
        answer_callback_query(cq_id)

    elif data.startswith("score:zero:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        db.zero_out_user_score(target_id)
        u = db.get_user(target_id)
        name = u["display_name"] if u else str(target_id)
        edit_message_text(chat_id, message_id, f"👤 {name}\nامتیاز فعلی: 0\n\nچی‌کار کنم؟", score_user_detail_menu(target_id))
        answer_callback_query(cq_id, "0️⃣ امتیاز صفر شد.")

    elif data.startswith("score:remove_confirm:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        db.remove_user_from_leaderboard(target_id)
        edit_message_text(chat_id, message_id, "🎛 مدیریت امتیازات\n\nیک نفر را انتخاب کن:", score_manage_list_menu())
        answer_callback_query(cq_id, "🗑 کامل حذف شد.")

    elif data.startswith("score:remove:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        u = db.get_user(target_id)
        name = u["display_name"] if u else str(target_id)
        edit_message_text(
            chat_id, message_id, f"⚠️ مطمئنی می‌خوای «{name}» رو کامل از رتبه‌بندی حذف کنی؟\nاین کار قابل بازگشت نیست.",
            score_remove_confirm_menu(target_id),
        )
        answer_callback_query(cq_id)

    # ---------------- تنظیمات ---------------- #
    elif data == "menu:settings":
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "⚙️ تنظیمات ربات", settings_menu())
        answer_callback_query(cq_id)

    elif data == "settings:toggle_show_correct":
        if not require_admin():
            return
        current = db.get_setting("show_correct_answer", "1") == "1"
        db.set_setting("show_correct_answer", "0" if current else "1")
        edit_message_text(chat_id, message_id, "⚙️ تنظیمات ربات", settings_menu())
        answer_callback_query(cq_id, "✅ به‌روزرسانی شد.")

    elif data == "menu:score_formula":
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "🧮 فرمول نمره‌دهی\n\nحالت موردنظر را انتخاب کن:", score_formula_menu())
        answer_callback_query(cq_id)

    elif data.startswith("formula:"):
        if not require_admin():
            return
        count = int(data.split(":")[1])
        base, low, high = db.get_score_config(count)
        set_state(chat_id, user_id, "edit_formula", count=count)
        if count == 1:
            explain = "برای حالت ۱ سؤالی، «جریمه‌ی کوچک» یعنی کمترین جریمه (پاسخ سریع) و «جریمه‌ی بزرگ» یعنی بیشترین جریمه (پاسخ در آخرین لحظه)."
        else:
            explain = "پاسخ سریع (یک‌سومِ اول زمان) هیچ جریمه‌ای نمی‌گیرد؛ «جریمه‌ی کوچک» برای یک‌سومِ میانی و «جریمه‌ی بزرگ» برای یک‌سومِ آخر اعمال می‌شود."
        edit_message_text(
            chat_id, message_id,
            f"🧮 فرمول {count} سؤالی\n\nمقدار فعلی: نمره‌ی پایه={fmt_score(base)}، جریمه‌ی کوچک={fmt_score(low)}، جریمه‌ی بزرگ={fmt_score(high)}\n\n"
            f"{explain}\n\nسه عدد را با فاصله بفرست، به همین ترتیب: نمره_پایه جریمه_کوچک جریمه_بزرگ\nمثال: {fmt_score(base)} {fmt_score(low)} {fmt_score(high)}",
        )
        answer_callback_query(cq_id)

    # ---------------- مدیریت ادمین‌ها ---------------- #
    elif data == "menu:admins":
        if not require_admin():
            return
        edit_message_text(chat_id, message_id, "👤 مدیریت ادمین‌ها\n\nادمین‌های ثابت (🔒) از طریق فایل .env تنظیم می‌شوند و از اینجا قابل حذف نیستند.", admins_menu())
        answer_callback_query(cq_id)

    elif data == "admin:add":
        if not require_admin():
            return
        set_state(chat_id, user_id, "add_admin_id")
        edit_message_text(chat_id, message_id, "🔢 آیدی عددی تلگرام ادمین جدید را ارسال کن:\n(کاربر موردنظر می‌تواند از @userinfobot آیدی‌اش را بگیرد)")
        answer_callback_query(cq_id)

    elif data.startswith("admin:remove:"):
        if not require_admin():
            return
        target_id = int(data.split(":")[2])
        if target_id in ADMIN_IDS:
            answer_callback_query(cq_id, "🔒 این ادمین ثابت است و از داخل ربات قابل حذف نیست.", show_alert=True)
            return
        db.remove_admin(target_id)
        edit_message_text(chat_id, message_id, "👤 مدیریت ادمین‌ها", admins_menu())
        answer_callback_query(cq_id, "✅ حذف شد.")


    else:
        answer_callback_query(cq_id)


def _handle_answer(data: str, chat_id: int, user_id: int, cq_id: str) -> None:
    _, qq_id_str, option_id_str = data.split(":")
    quiz_question_id, option_id = int(qq_id_str), int(option_id_str)

    qq = db.get_quiz_question(quiz_question_id)
    if qq is None:
        answer_callback_query(cq_id, "❌ این سؤال یافت نشد.", show_alert=True)
        return
    quiz = db.get_quiz(qq["quiz_id"])
    if quiz is None or quiz["status"] != "question_active" or qq["is_finished"]:
        answer_callback_query(cq_id, "❌ زمان پاسخ‌گویی به این سؤال تمام شده است.", show_alert=True)
        return
    if is_paused(quiz["id"]):
        answer_callback_query(cq_id, "⏸ کوییز موقتاً متوقف شده. یه‌کم صبر کن.", show_alert=True)
        return
    if not db.is_participant(quiz["id"], user_id):
        answer_callback_query(cq_id, "❌ شما در این کوییز شرکت نکرده‌اید.", show_alert=True)
        return
    if qq["started_at"] is None:
        answer_callback_query(cq_id, "❌ این سؤال هنوز شروع نشده است.", show_alert=True)
        return

    started_at = datetime.fromisoformat(qq["started_at"])
    response_time = (datetime.now(timezone.utc) - started_at).total_seconds()
    if response_time > qq["duration_seconds"] + 1:
        answer_callback_query(cq_id, "❌ زمان پاسخ‌گویی به این سؤال تمام شده است.", show_alert=True)
        return

    selected = next((o for o in qq["question"]["options"] if o["id"] == option_id), None)
    if selected is None:
        answer_callback_query(cq_id, "❌ گزینه نامعتبر است.", show_alert=True)
        return

    score = compute_score(bool(selected["is_correct"]), response_time, qq["duration_seconds"], quiz["question_count"])
    ok = db.record_answer(
        quiz["id"], qq["id"], user_id, option_id, bool(selected["is_correct"]), response_time, score,
    )
    if not ok:
        answer_callback_query(cq_id, "⚠️ شما قبلاً به این سؤال پاسخ داده‌اید.", show_alert=True)
        return
    answer_callback_query(cq_id, "✅ پاسخ ثبت شد!")


# --------------------------------------------------------------------- #
# حلقه اصلی Polling
# --------------------------------------------------------------------- #

def _read_offset() -> int:
    try:
        with open(OFFSET_FILE, "r") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _write_offset(offset: int) -> None:
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except Exception:
        log.warning("Could not persist offset to %s", OFFSET_FILE)


def handle_update(update: dict) -> None:
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception:
        log.exception("Error processing update")


def main() -> None:
    db.init_db()
    db.seed_admins_from_env(ADMIN_IDS)
    log.info("Database ready.")
    recover_all()
    log.info("Recovery pass complete. Starting polling...")

    offset = _read_offset()
    while True:
        try:
            updates = get_updates(offset, poll_timeout=30)
        except Exception:
            log.exception("Polling error, retrying in 3s")
            time.sleep(3)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            threading.Thread(target=handle_update, args=(update,), daemon=True).start()

        if updates:
            _write_offset(offset)


if __name__ == "__main__":
    main()
