import telebot
from telebot import types
import os
import logging
from flask import Flask, request
import time
import requests
import json

# =========================
# 📝 ЛОГУВАННЯ
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# =========================
# 🔐 НАЛАШТУВАННЯ (з Render env)
# =========================
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "ТВІЙ_ТОКЕН_БОТА")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "887078537"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://78655.onrender.com")
TURSO_URL   = os.getenv("TURSO_URL",   "libsql://yhbvgt656-yhbvgt656.aws-eu-west-1.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJleHAiOjE4NDM0OTA5MTksImdpZCI6ImZmYTlmM2NjLWZlY2ItNGU1Zi1hNzA5LTZjNTJlNzFjY2NhYSIsImlhdCI6MTc3NjQ0NDUxOSwicmlkIjoiOTBkMDM1ZmQtZGY5Ni00MGE3LTg1YmYtMDM4ZmQ1MDAyMDkyIn0.own5cPQDw4Jfm_P-Ql8EdC_OYfRCI-pkXgCS_9vJkh6Y0SxxWAURIsbzDD8e0P5R-eTdc4uCkVV1nwhbRjiVAA")
MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 2

# =========================
# 📊 СТАНИ СЕСІЙ (в пам'яті)
# =========================
user_states  = {}   # {chat_id: "state_name"}
user_form    = {}   # {chat_id: {phone, name, level, trainer_id, trainer_name, trainer_username}}
trainer_form = {}   # {chat_id: {username, name, description}} або тимчасові дані адміна
admin_chats  = {}   # {user_chat_id: admin_chat_id}

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

@app.route("/")
def uptime_check():
    return "OK", 200

# ==========================================================
# 🗄️  TURSO DATABASE LAYER
# ==========================================================
def _unpack_turso_value(v):
    if not isinstance(v, dict):
        return v
    t, val = v.get("type", ""), v.get("value")
    if val is None or t == "null":
        return None
    if t == "integer":
        try: return int(val)
        except: return val
    if t == "float":
        try: return float(val)
        except: return val
    return val


class QueryResult:
    def __init__(self, rows=None):
        self.rows = []
        if not rows:
            return
        first = rows[0]
        if isinstance(first, dict) and "values" in first:
            self.rows = [tuple(_unpack_turso_value(v) for v in r.get("values", [])) for r in rows]
        elif isinstance(first, dict):
            self.rows = [tuple(r.values()) for r in rows]
        else:
            self.rows = [tuple(r) if not isinstance(r, tuple) else r for r in rows]


class TursoClient:
    def __init__(self, url: str, auth_token: str):
        if url.startswith("libsql://"):
            url = url.replace("libsql://", "https://", 1)
        self.url     = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}

    def execute(self, sql: str, args: list = None) -> QueryResult:
        stmt = {"sql": sql}
        if args:
            turso_args = []
            for a in args:
                if a is None:              turso_args.append({"type": "null",    "value": None})
                elif isinstance(a, bool):  turso_args.append({"type": "integer", "value": str(int(a))})
                elif isinstance(a, int):   turso_args.append({"type": "integer", "value": str(a)})
                elif isinstance(a, float): turso_args.append({"type": "float",   "value": str(a)})
                else:                      turso_args.append({"type": "text",    "value": str(a)})
            stmt["args"] = turso_args

        payload = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
        resp    = requests.post(f"{self.url}/v2/pipeline", json=payload, headers=self.headers, timeout=10)
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data    = resp.json() 
        results = data.get("results", [])
        if not results:
            return QueryResult([])

        first = results[0]
        if first.get("type") == "error":
            err = first.get("error", {})
            raise Exception(f"DB Error: {err.get('message', str(err)) if isinstance(err, dict) else err}")

        response_obj = first.get("response", {})
        result_inner = response_obj.get("result", {}) if isinstance(response_obj, dict) else {}
        rows         = result_inner.get("rows") if isinstance(result_inner, dict) else None
        if rows is None and isinstance(response_obj, dict):
            rows = response_obj.get("rows")

        logger.info(f"✅ SQL OK — {len(rows) if rows else 0} rows")
        return QueryResult(rows or [])


_client: TursoClient = None


def _init_client() -> bool:
    global _client
    try:
        _client = TursoClient(url=TURSO_URL, auth_token=TURSO_TOKEN)
        _client.execute("SELECT 1")
        logger.info("✅ Підключено до Turso")
        return True
    except Exception as e:
        logger.error(f"❌ _init_client: {e}")
        _client = None
        return False


def get_db(retry: int = 0) -> TursoClient:
    global _client
    if _client is None:
        if retry >= MAX_DB_RETRIES:
            return None
        time.sleep(DB_RETRY_DELAY)
        _init_client()
        return get_db(retry + 1)
    try:
        _client.execute("SELECT 1")
        return _client
    except Exception:
        _client = None
        return get_db(retry)


def init_db() -> bool:
    db = get_db()
    if not db:
        return False
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS trainers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                description TEXT,
                telegram_id INTEGER,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Додаємо колонку якщо таблиця вже існувала без неї
        try:
            db.execute("ALTER TABLE trainers ADD COLUMN telegram_id INTEGER")
        except Exception:
            pass  # колонка вже є — ігноруємо
        logger.info("✅ Таблиця trainers готова")
        return True
    except Exception as e:
        logger.error(f"❌ init_db: {e}")
        return False


# ==========================================================
# 🛠️  КОНСТАНТИ І ДОПОМІЖНІ ФУНКЦІЇ
# ==========================================================
BTN_CANCEL = "❌ Скасувати"
BTN_EDIT   = "⚙️ Edit"
BTN_BACK   = "⬅️ Назад"

ADMIN_RESERVED = {
    BTN_EDIT, BTN_BACK, BTN_CANCEL,
    "➕ Додати тренера", "➖ Видалити тренера", "📋 Список тренерів",
}


# ==========================================================
# 🏅  БЕЙДЖІ РІВНІВ — красиве відображення рівня гравця
# ==========================================================
LEVEL_BADGE = {
    "🌱 Початківець": "🌱 Початківець  │ навчаємось азам",
    "🎯 Аматор":      "🎯 Аматор       │ знає основи",
    "⚔️ Просунутий":  "⚔️ Просунутий   │ серйозний гравець",
    "👑 Експерт":     "👑 Експерт      │ майстер дошки",
}

# ==========================================================
# ✨  WOW-ЕФЕКТ — преміум лоадер для кожної дії
#
#  Принцип роботи:
#  1. bot.send_chat_action()  →  Telegram показує "друкує..."
#  2. bot.send_message()      →  відправляємо placeholder-повідомлення з текстом лоадера
#  3. time.sleep(tiny_delay)  →  мікро-пауза для ефекту "обробки"
#  4. bot.edit_message_text() →  замінюємо placeholder на реальний результат
#     (або просто видаляємо — залежно від use-case)
#
#  wow_action(chat_id, action)  →  відправляє placeholder, повертає message_id
#  wow_done(chat_id, msg_id, text, **kwargs)  →  редагує placeholder в результат
#  wow_delete(chat_id, msg_id)  →  тихо видаляє placeholder
# ==========================================================

# Тексти лоадерів під кожну дію — щоб виглядало "розумно"
_WOW_TEXTS = {
    "search":   "🔍  _Шукаємо для вас..._",
    "save":     "💾  _Зберігаємо дані..._",
    "send":     "📨  _Відправляємо заявку..._",
    "load":     "⚡️  _Завантажуємо..._",
    "process":  "⚙️  _Обробляємо..._",
    "check":    "🛡  _Перевіряємо..._",
    "connect":  "🔗  _Підключаємось..._",
    "welcome":  "✨  _Готуємо для вас..._",
}

# chat_action для різних типів дій (Telegram показує різні анімації)
_WOW_ACTIONS = {
    "search":   "typing",
    "save":     "upload_document",
    "send":     "typing",
    "load":     "typing",
    "process":  "upload_document",
    "check":    "typing",
    "connect":  "typing",
    "welcome":  "typing",
}


def wow_action(chat_id: int, kind: str = "load") -> int | None:
    """
    Відправляє chat_action + placeholder-повідомлення.
    Повертає message_id placeholder або None при помилці.
    """
    try:
        bot.send_chat_action(chat_id, _WOW_ACTIONS.get(kind, "typing"))
    except Exception:
        pass
    try:
        msg = bot.send_message(chat_id, _WOW_TEXTS.get(kind, "⏳  _Зачекайте..._"),
                               parse_mode="Markdown")
        return msg.message_id
    except Exception:
        return None


def wow_done(chat_id: int, msg_id: int | None,
             text: str, parse_mode: str = None,
             reply_markup=None):
    """
    Редагує placeholder в фінальне повідомлення.
    Якщо редагування не вдалось — відправляє нове (fallback).
    """
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id,
                                  parse_mode=parse_mode,
                                  reply_markup=reply_markup)
            return
        except Exception:
            pass
    # fallback: просто відправляємо нове
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode,
                         reply_markup=reply_markup)
    except Exception:
        pass


def wow_delete(chat_id: int, msg_id: int | None):
    """Тихо видаляє placeholder якщо він більше не потрібен."""
    if not msg_id:
        return
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


# Зворотна сумісність — стара назва typing() теж працює
def typing(chat_id: int):
    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass


def main_menu_markup(user_id: int) -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("♟️ Вибрати тренера", "👨‍🏫 Наші тренери")
    m.add("💬 Зв'язатися з адміністратором")
    if user_id == ADMIN_ID:
        m.add(BTN_EDIT)
    return m


def admin_menu_markup() -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("➕ Додати тренера", "➖ Видалити тренера")
    m.add("📋 Список тренерів")
    m.add(BTN_BACK)
    return m


def cancel_only_markup() -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add(BTN_CANCEL)
    return m


def send_main_menu(chat_id: int, user_id: int, text: str = "Головне меню:", parse_mode: str = None):
    """
    ВИПРАВЛЕННЯ №1: user_form.pop() ВИДАЛЕНО звідси.
    Дані заявки зберігаються до підтвердження/відхилення адміном.
    Очищення відбувається тільки в admin_confirm_enroll та admin_write_reject_reason.
    """
    user_states.pop(chat_id, None)
    # user_form.pop(chat_id, None)  ← ВИДАЛЕНО: призводило до втрати даних заявки
    trainer_form.pop(chat_id, None)
    user_states[chat_id] = "main_menu"
    bot.send_message(chat_id, text, reply_markup=main_menu_markup(user_id), parse_mode=parse_mode)


def reset_to_main(message):
    """Універсальне скасування — повертає у головне меню."""
    cid = message.chat.id
    uid = message.from_user.id

    if uid == ADMIN_ID:
        partner_id = next((u for u, a in admin_chats.items() if a == uid), None)
        if partner_id:
            admin_chats.pop(partner_id, None)
            user_states.pop(partner_id, None)
            try:
                bot.send_message(partner_id, "👋 Адміністратор завершив чат.",
                                 reply_markup=main_menu_markup(partner_id))
            except Exception:
                pass
    elif cid in admin_chats:
        admin_id = admin_chats.pop(cid)
        try:
            bot.send_message(admin_id, "👤 Користувач завершив чат.")
        except Exception:
            pass

    send_main_menu(cid, uid, "↩️ Повернення до головного меню.")


def format_trainer_card(name: str, username: str, desc: str, index: int = None) -> str:
    """Форматує картку тренера — оптимізовано для мобільних."""
    num       = f"*{index}*  " if index else ""
    desc_text = desc if desc else "Опис не вказано"
    return (
        f"{num}👨\u200d🏫 *{name}*\n"
        f"╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n"
        f"📎 @{username}\n"
        f"📝 _{desc_text}_"
    )


# ==========================================================
# 🏁  /start
# ==========================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    cid        = message.chat.id
    uid        = message.from_user.id
    first_name = message.from_user.first_name or "друже"
    loader = wow_action(cid, "welcome")
    welcome = (
        "♟️ *Шахова школа*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        f"Вітаємо, *{first_name}*\\! 👋\n\n"
        "Тут ви можете:\n"
        "› Обрати тренера та записатися\n"
        "› Переглянути наших фахівців\n"
        "› Зв'язатися з адміністратором\n\n"
        "⬇️ _Оберіть дію нижче_"
    )
    wow_delete(cid, loader)
    send_main_menu(cid, uid, welcome, parse_mode="MarkdownV2")


# ==========================================================
# ❌  УНІВЕРСАЛЬНЕ СКАСУВАННЯ  (реєструється першим!)
# ==========================================================
@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def universal_cancel(message):
    reset_to_main(message)


# ==========================================================
# 👨‍🏫  НАШІ ТРЕНЕРИ (публічна кнопка в головному меню)
# ==========================================================
@bot.message_handler(func=lambda m: m.text == "👨‍🏫 Наші тренери")
def show_our_trainers(message):
    cid    = message.chat.id
    loader = wow_action(cid, "search")
    db = get_db()
    if not db:
        wow_done(cid, loader, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute(
            "SELECT id, name, username, description FROM trainers ORDER BY name"
        ).rows
    except Exception as e:
        wow_done(cid, loader, f"❌ Помилка: {e}")
        return

    if not trainers:
        wow_done(cid, loader,
                 "😔 Тренерів поки немає. Перевірте пізніше!",
                 reply_markup=main_menu_markup(message.from_user.id))
        return

    wow_done(cid, loader,
             f"👨\u200d🏫 *Наші тренери*\n"
             f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             f"Знайдено фахівців: *{len(trainers)}*",
             parse_mode="Markdown")
    for i, row in enumerate(trainers, 1):
        name  = _unpack_turso_value(row[1])
        uname = _unpack_turso_value(row[2])
        desc  = _unpack_turso_value(row[3])
        name  = str(name)  if name  else "Без імені"
        uname = str(uname) if uname else "no_username"
        desc  = str(desc)  if desc  else "Опис не вказано"
        card  = format_trainer_card(name, uname, desc, index=i)
        bot.send_message(cid, card, parse_mode="Markdown")

    bot.send_message(
        cid,
        "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n"
        "🎯 Готові розпочати?\n"
        "Натисніть *♟️ Вибрати тренера* щоб записатись\\!",
        parse_mode="MarkdownV2",
        reply_markup=main_menu_markup(message.from_user.id)
    )


# ==========================================================
# 👨‍💼  АДМІН-ПАНЕЛЬ
# ==========================================================
@bot.message_handler(func=lambda m: m.text == BTN_EDIT)
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Немає доступу.")
        return
    typing(message.chat.id)
    user_states[message.chat.id] = "admin_panel"
    bot.send_message(
        message.chat.id,
        "⚙️ *Адмін\\-панель*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "Оберіть дію:",
        parse_mode="MarkdownV2",
        reply_markup=admin_menu_markup()
    )


@bot.message_handler(func=lambda m: m.text == BTN_BACK)
def admin_back(message):
    send_main_menu(message.chat.id, message.from_user.id, "🔙 Повернення до меню.")


# ── ДОДАТИ ТРЕНЕРА ─────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "➕ Додати тренера")
def add_trainer_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    trainer_form[message.chat.id] = {}
    user_states[message.chat.id]  = "add_trainer_forward"
    bot.send_message(
        message.chat.id,
        "➕ *Додавання тренера*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "🔵 Крок 1 з 3  ●○○\n\n"
        "📨 *Перешліть* боту будь-яке повідомлення від тренера\n\n"
        "Як це зробити:\n"
        "1\\. Відкрийте чат з тренером у Telegram\n"
        "2\\. Натисніть і утримуйте будь-яке його повідомлення\n"
        "3\\. Оберіть *«Переслати»* → оберіть цього бота\n\n"
        "📌 _Бот автоматично отримає Telegram ID та @username тренера_",
        parse_mode="MarkdownV2",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(
    func=lambda m: user_states.get(m.chat.id) == "add_trainer_forward"
)
def add_trainer_forward(message):
    cid = message.chat.id

    # Перевіряємо що це пересланe повідомлення
    fwd = message.forward_from  # None якщо людина приховала акаунт в налаштуваннях

    if not fwd:
        # Якщо forward_from відсутній — конфіденційність акаунту закрита
        bot.send_message(
            cid,
            "⚠️ *Не вдалося отримати дані*\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            "Тренер закрив доступ до свого акаунту в налаштуваннях Telegram "
            "\\(_Конфіденційність → Переслані повідомлення_\\)\\.\n\n"
            "Попросіть тренера:\n"
            "› Відкрити *Налаштування → Конфіденційність*\n"
            "› *Переслані повідомлення* → встановити *«Всі»* або *«Мої контакти»*\n"
            "› Після цього перешліть повідомлення ще раз\n\n"
            "_Або введіть @username тренера вручну нижче, але тоді ID не збережеться:_",
            parse_mode="MarkdownV2",
            reply_markup=cancel_only_markup()
        )
        # Перемикаємо на ручне введення як запасний варіант
        user_states[cid] = "add_trainer_username_manual"
        return

    tg_id   = fwd.id
    uname   = fwd.username or ""
    display = f"@{uname}" if uname else f"ID {tg_id}"
    db_username = uname if uname else str(tg_id)

    trainer_form[cid].update({
        "telegram_id":      tg_id,
        "username":         db_username,
        "display_username": display,
    })
    user_states[cid] = "add_trainer_name"
    bot.send_message(
        cid,
        f"➕ *Додавання тренера*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🔵 Крок 2 з 3  ●●○\n\n"
        f"✅ Дані отримано\\!\n"
        f"👤 {display}\n"
        f"🆔 `{tg_id}`\n\n"
        f"✍️ Введіть повне ім'я тренера\n\n"
        f"📌 _Приклад:_ `Іван Петренко`",
        parse_mode="MarkdownV2",
        reply_markup=cancel_only_markup()
    )


# Запасний варіант — ручне введення @username якщо forward заблоковано
@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_username_manual")
def add_trainer_username_manual(message):
    cid      = message.chat.id
    username = message.text.strip()
    if not username.startswith("@"):
        bot.send_message(cid, "❌ Username має починатися з @\\. Спробуйте ще раз:", parse_mode="MarkdownV2")
        return
    uname   = username[1:]
    display = f"@{uname}"
    trainer_form[cid].update({
        "telegram_id":      None,  # невідомий — повідомлення будуть по @username
        "username":         uname,
        "display_username": display,
    })
    user_states[cid] = "add_trainer_name"
    bot.send_message(
        cid,
        f"➕ *Додавання тренера*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🔵 Крок 2 з 3  ●●○\n\n"
        f"✅ Username збережено: *{display}*\n"
        f"⚠️ _ID не отримано — сповіщення будуть по @username_\n\n"
        f"✍️ Введіть повне ім'я тренера\n\n"
        f"📌 _Приклад:_ `Іван Петренко`",
        parse_mode="MarkdownV2",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_name")
def add_trainer_name(message):
    trainer_form[message.chat.id]["name"] = message.text.strip()
    user_states[message.chat.id] = "add_trainer_description"
    bot.send_message(
        message.chat.id,
        "➕ *Додавання тренера*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "🔵 Крок 3 з 3  ●●●\n\n"
        "Введіть опис тренера\n\n"
        "📌 _Вкажіть досвід, звання, спеціалізацію_",
        parse_mode="Markdown",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_description")
def add_trainer_description(message):
    cid  = message.chat.id
    data = trainer_form.get(cid, {})
    data["description"] = message.text.strip()
    loader = wow_action(cid, "save")
    db = get_db()
    if not db:
        wow_done(cid, loader, "❌ Помилка підключення до БД.", reply_markup=admin_menu_markup())
        user_states.pop(cid, None)
        trainer_form.pop(cid, None)
        return
    try:
        db.execute(
            "INSERT INTO trainers (username, name, description, telegram_id) VALUES (?, ?, ?, ?)",
            [data["username"], data["name"], data["description"], data.get("telegram_id")]
        )
        wow_done(cid, loader,
                 f"✅ *Тренер доданий\\!*\n"
                 f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                 f"👨\u200d🏫 *{data['name']}*\n"
                 f"📎 {data['display_username']}\n\n"
                 f"_Тренер з'явиться у списку одразу\\._",
                 parse_mode="MarkdownV2",
                 reply_markup=admin_menu_markup())
        logger.info(f"✅ Додано тренера: {data['name']}")
    except Exception as e:
        if "unique" in str(e).lower() or "constraint" in str(e).lower():
            wow_done(cid, loader,
                     f"⚠️ Тренер {data['display_username']} вже існує в базі.",
                     reply_markup=admin_menu_markup())
        else:
            wow_done(cid, loader, f"❌ Помилка: {e}", reply_markup=admin_menu_markup())
    user_states.pop(cid, None)
    trainer_form.pop(cid, None)


# ── ВИДАЛИТИ ТРЕНЕРА ────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "➖ Видалити тренера")
def delete_trainer_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    cid    = message.chat.id
    loader = wow_action(cid, "search")
    db = get_db()
    if not db:
        wow_done(cid, loader, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute("SELECT id, name, username FROM trainers ORDER BY name").rows
    except Exception as e:
        wow_done(cid, loader, f"❌ Помилка: {e}")
        return
    if not trainers:
        wow_done(cid, loader, "📭 Тренерів немає.", reply_markup=admin_menu_markup())
        return

    wow_done(cid, loader,
             f"🗑 *Видалення тренера*\n"
             f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             f"Тренерів у базі: *{len(trainers)}*\n\n"
             f"⚠️ _Буде запит на підтвердження перед видаленням_",
             parse_mode="Markdown")
    for row in trainers:
        tid_raw = _unpack_turso_value(row[0])
        try:
            tid = int(tid_raw)
        except Exception:
            continue
        name  = _unpack_turso_value(row[1])
        uname = _unpack_turso_value(row[2])
        name  = str(name)  if name  is not None else "Без імені"
        uname = str(uname) if uname is not None else "no_username"

        card = f"👨‍🏫 {name}\n📎 @{uname}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🗑 Вилучити", callback_data=f"delete_{tid}"))
        bot.send_message(message.chat.id, card, reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("delete_"))
def delete_trainer_handler(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Немає доступу", show_alert=True)
        return
    try:
        tid = int(call.data.split("_", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    db = get_db()
    if not db:
        bot.answer_callback_query(call.id, "❌ Помилка БД", show_alert=True)
        return
    try:
        result = db.execute("SELECT name, username FROM trainers WHERE id = ?", [tid])
        if not result.rows:
            bot.answer_callback_query(call.id, "❌ Тренера не знайдено", show_alert=True)
            return
        name_raw  = result.rows[0][0]
        uname_raw = result.rows[0][1]
        name  = str(_unpack_turso_value(name_raw)  if isinstance(name_raw,  dict) else name_raw)
        uname = str(_unpack_turso_value(uname_raw) if isinstance(uname_raw, dict) else uname_raw)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:80]}", show_alert=True)
        return

    # Показуємо картку з підтвердженням — НЕ видаляємо одразу
    confirm_markup = types.InlineKeyboardMarkup(row_width=2)
    confirm_markup.add(
        types.InlineKeyboardButton("✅ Так, видалити", callback_data=f"confirm_delete_{tid}"),
        types.InlineKeyboardButton("❌ Скасувати",     callback_data=f"cancel_delete_{tid}")
    )
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"🗑 *Видалення тренера*\n"
        f"╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n"
        f"👨\u200d🏫 *{name}*\n"
        f"📎 @{uname}\n\n"
        f"⚠️ Ви впевнені\\? Це незворотня дія\\.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="MarkdownV2",
        reply_markup=confirm_markup
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_delete_"))
def confirm_delete_trainer(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Немає доступу", show_alert=True)
        return
    try:
        tid = int(call.data.split("_", 2)[2])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    db = get_db()
    if not db:
        bot.answer_callback_query(call.id, "❌ Помилка БД", show_alert=True)
        return
    try:
        result = db.execute("SELECT name FROM trainers WHERE id = ?", [tid])
        if not result.rows:
            bot.answer_callback_query(call.id, "❌ Тренера вже немає в базі", show_alert=True)
            bot.edit_message_text(
                "ℹ️ Тренера вже було видалено раніше.",
                call.message.chat.id, call.message.message_id
            )
            return
        name_raw = result.rows[0][0]
        name = str(_unpack_turso_value(name_raw) if isinstance(name_raw, dict) else name_raw)

        db.execute("DELETE FROM trainers WHERE id = ?", [tid])
        logger.info(f"🗑 Видалено тренера: {name} (id={tid})")

        bot.answer_callback_query(call.id, f"✅ {name} видалений!")
        bot.edit_message_text(
            f"✅ *Готово\\!*\n"
            f"╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n"
            f"Тренер *{name}* видалений з бази\\.",
            call.message.chat.id, call.message.message_id,
            parse_mode="MarkdownV2"
        )
        bot.send_message(call.message.chat.id, "Що далі?", reply_markup=admin_menu_markup())
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:80]}", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_delete_"))
def cancel_delete_trainer(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id, "↩️ Скасовано")
    bot.edit_message_text(
        "↩️ Видалення скасовано\\.",
        call.message.chat.id, call.message.message_id,
        parse_mode="MarkdownV2"
    )
    bot.send_message(call.message.chat.id, "Повертаємось до меню:", reply_markup=admin_menu_markup())


# ── СПИСОК ТРЕНЕРІВ (адмін) ─────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📋 Список тренерів")
def list_trainers(message):
    if message.from_user.id != ADMIN_ID:
        return
    cid    = message.chat.id
    loader = wow_action(cid, "search")
    db = get_db()
    if not db:
        wow_done(cid, loader, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute(
            "SELECT id, name, username, description FROM trainers ORDER BY name"
        ).rows
    except Exception as e:
        wow_done(cid, loader, f"❌ Помилка: {e}")
        return
    if not trainers:
        wow_done(cid, loader, "📭 Список тренерів порожній.")
        return

    wow_done(cid, loader,
             f"📋 *Тренери в базі*\n"
             f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             f"Знайдено: *{len(trainers)}* чол\\.",
             parse_mode="MarkdownV2")
    for i, row in enumerate(trainers, 1):
        name  = _unpack_turso_value(row[1])
        uname = _unpack_turso_value(row[2])
        desc  = _unpack_turso_value(row[3])
        name  = str(name)  if name  else "Без імені"
        uname = str(uname) if uname else "no_username"
        desc  = str(desc)  if desc  else "Опис не вказано"
        card  = format_trainer_card(name, uname, desc, index=i)
        bot.send_message(message.chat.id, card, parse_mode="Markdown")


# ==========================================================
# 👤  ВИБІР ТРЕНЕРА (користувач)
# ==========================================================
@bot.message_handler(func=lambda m: m.text == "♟️ Вибрати тренера")
def choose_trainer_start(message):
    cid = message.chat.id
    loader = wow_action(cid, "load")
    user_states[cid] = "user_waiting_age_type"
    user_form[cid]   = {}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🧑 Дорослий", "👦 Дитина")
    markup.add(BTN_CANCEL)
    wow_done(cid, loader,
             "♟️ *Запис до тренера*\n"
             "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             "🔵 Крок 1 з 6  ●○○○○○\n\n"
             "👤 Для кого записуєтесь?",
             parse_mode="Markdown",
             reply_markup=markup)


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_age_type")
def user_got_age_type(message):
    if message.text not in {"🧑 Дорослий", "👦 Дитина"}:
        bot.send_message(message.chat.id, "Оберіть варіант із кнопок нижче.")
        return
    cid = message.chat.id
    user_form[cid]["age_type"] = message.text
    user_states[cid] = "user_waiting_phone"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📱 Поділитися номером", request_contact=True))
    markup.add(BTN_CANCEL)
    bot.send_message(
        cid,
        "♟️ *Запис до тренера*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "🔵 Крок 2 з 6  ●●○○○○\n\n"
        "📱 Поділіться вашим номером телефону\n\n"
        "_Натисніть кнопку нижче — це безпечно і потрібно для зв'язку з тренером_",
        parse_mode="Markdown",
        reply_markup=markup)


@bot.message_handler(content_types=["contact"])
def user_got_phone(message):
    if user_states.get(message.chat.id) != "user_waiting_phone":
        return
    cid    = message.chat.id
    loader = wow_action(cid, "check")
    user_form[cid]["phone"] = message.contact.phone_number
    user_states[cid] = "user_waiting_lesson_type"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("👥 Групове заняття", "👤 Індивідуальне заняття")
    markup.add(BTN_CANCEL)
    wow_done(cid, loader,
             "♟️ *Запис до тренера*\n"
             "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             "🔵 Крок 3 з 6  ●●●○○○\n\n"
             "✅ Номер збережено\\!\n\n"
             "📚 Який формат занять вас цікавить?",
             parse_mode="MarkdownV2",
             reply_markup=markup)


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_lesson_type")
def user_got_lesson_type(message):
    if message.text not in {"👥 Групове заняття", "👤 Індивідуальне заняття"}:
        bot.send_message(message.chat.id, "Оберіть варіант із кнопок нижче.")
        return
    cid = message.chat.id
    user_form[cid]["lesson_type"] = message.text
    user_states[cid] = "user_waiting_name"
    bot.send_message(
        cid,
        "♟️ *Запис до тренера*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "🔵 Крок 4 з 6  ●●●●○○\n\n"
        "✍️ Введіть ваше ім'я та прізвище",
        parse_mode="Markdown",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_name")
def user_got_name(message):
    cid    = message.chat.id
    loader = wow_action(cid, "check")
    user_form[cid]["name"] = message.text.strip()
    user_states[cid] = "user_waiting_level"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🌱 Початківець", "🎯 Аматор")
    markup.row("⚔️ Просунутий",  "👑 Експерт")
    markup.add(BTN_CANCEL)
    wow_done(cid, loader,
             "♟️ *Запис до тренера*\n"
             "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             "🔵 Крок 5 з 6  ●●●●●○\n\n"
             "♟️ Оберіть ваш рівень гри в шахи:",
             parse_mode="Markdown",
             reply_markup=markup)


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_level")
def user_got_level(message):
    allowed = {"🌱 Початківець", "🎯 Аматор", "⚔️ Просунутий", "👑 Експерт"}
    if message.text not in allowed:
        bot.send_message(message.chat.id, "Оберіть рівень із кнопок нижче.")
        return
    cid        = message.chat.id
    loader     = wow_action(cid, "search")
    user_form[cid]["level"] = message.text
    level_badge = LEVEL_BADGE.get(message.text, message.text)

    db = get_db()
    if not db:
        wow_done(cid, loader, "❌ Помилка підключення до БД.")
        reset_to_main(message)
        return
    try:
        trainers = db.execute(
            "SELECT id, name, description FROM trainers ORDER BY name"
        ).rows
    except Exception as e:
        wow_done(cid, loader, f"❌ Помилка: {e}")
        reset_to_main(message)
        return

    if not trainers:
        wow_done(cid, loader,
                 "😔 Тренерів поки немає. Спробуйте пізніше.",
                 reply_markup=main_menu_markup(message.from_user.id))
        user_states.pop(cid, None)
        user_form.pop(cid, None)
        return

    wow_done(cid, loader,
             f"✅ *Рівень збережено\\!*\n"
             f"`{level_badge}`\n\n"
             f"🔵 Крок 6 з 6  ●●●●●●\n\n"
             f"👇 *Оберіть тренера*\n"
             f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
             f"Натисніть кнопку під карткою тренера:",
             parse_mode="MarkdownV2",
             reply_markup=cancel_only_markup())

    for row in trainers:
        tid_raw  = _unpack_turso_value(row[0])
        name_raw = _unpack_turso_value(row[1])
        desc_raw = _unpack_turso_value(row[2])
        try:
            tid = int(tid_raw)
        except Exception:
            continue
        name = str(name_raw) if name_raw is not None else "Без імені"
        desc = str(desc_raw) if desc_raw else "Опис відсутній"

        card = (
            f"👨\u200d🏫 *{name}*\n"
            f"╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n"
            f"📝 _{desc}_"
        )
        pick_markup = types.InlineKeyboardMarkup()
        pick_markup.add(
            types.InlineKeyboardButton("📚 Записатися на курс", callback_data=f"pick_{tid}")
        )
        bot.send_message(message.chat.id, card, parse_mode="Markdown", reply_markup=pick_markup)

    user_states[message.chat.id] = "user_picking_trainer"


# ==========================================================
# ✅  ОБРАТИ ТРЕНЕРА → запит на підтвердження адміну
# ==========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("pick_"))
def user_picked_trainer(call):
    cid = call.message.chat.id

    try:
        tid = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    data = user_form.get(cid)
    if not data:
        bot.answer_callback_query(call.id, "❌ Сесія застаріла. Натисніть /start", show_alert=True)
        return

    db = get_db()
    if not db:
        bot.answer_callback_query(call.id, "❌ Помилка БД", show_alert=True)
        return
    try:
        result = db.execute("SELECT username, name, telegram_id FROM trainers WHERE id = ?", [tid]).rows
        if not result:
            bot.answer_callback_query(call.id, "❌ Тренер не знайдений", show_alert=True)
            return
        trainer_username = str(_unpack_turso_value(result[0][0]))
        trainer_name     = str(_unpack_turso_value(result[0][1]))
        trainer_tg_id    = _unpack_turso_value(result[0][2])  # може бути None якщо не збережено
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:50]}", show_alert=True)
        return

    # Зберігаємо дані тренера — потрібні адміну при підтвердженні
    data["trainer_id"]       = tid
    data["trainer_name"]     = trainer_name
    data["trainer_username"] = trainer_username
    data["trainer_tg_id"]    = trainer_tg_id

    # Показуємо "відправляємо..." прямо в картці тренера
    bot.answer_callback_query(call.id, "📨 Відправляємо заявку...")
    try:
        bot.edit_message_text(
            f"⚙️  _Відправляємо заявку..._",
            cid, call.message.message_id,
            parse_mode="Markdown"
        )
    except Exception:
        pass
    time.sleep(0.8)

    # Повідомлення учню — міняємо лоадер на результат
    try:
        bot.edit_message_text(
            f"📨 *Заявку надіслано\\!*\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"👨\u200d🏫 Тренер: *{trainer_name}*\n\n"
            f"⏳ _Очікуйте підтвердження адміністратора\\._\n"
            f"Ми одразу повідомимо вас\\!",
            cid, call.message.message_id,
            parse_mode="MarkdownV2"
        )
    except Exception:
        pass
    # НЕ видаляємо user_form тут — дані потрібні для підтвердження
    send_main_menu(cid, call.from_user.id, "Ми повідомимо вас!")

    # Попереднє повідомлення тренеру
    level_badge = LEVEL_BADGE.get(data['level'], data['level'])
    lesson_type = data.get('lesson_type', '—')
    age_type    = data.get('age_type', '—')
    trainer_msg = (
        f"📬 *Новий запит на заняття\\!*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"👤 Учень: *{data['name']}*\n"
        f"📱 `{data['phone']}`\n"
        f"🧒 {age_type}  │  📚 {lesson_type}\n"
        f"♟️ `{level_badge}`\n\n"
        f"_Очікуйте підтвердження від адміністратора\\._"
    )
    # Надсилаємо тренеру: спочатку за telegram_id, якщо є — це надійніше
    trainer_notified = False
    if trainer_tg_id:
        try:
            bot.send_message(int(trainer_tg_id), trainer_msg, parse_mode="MarkdownV2")
            logger.info(f"📨 Попередження тренеру ID={trainer_tg_id} надіслано")
            trainer_notified = True
        except Exception as e:
            logger.warning(f"⚠️ Не вдалося надіслати тренеру ID={trainer_tg_id}: {e}")
    if not trainer_notified and trainer_username:
        try:
            bot.send_message(f"@{trainer_username}", trainer_msg, parse_mode="MarkdownV2")
            logger.info(f"📨 Попередження тренеру @{trainer_username} надіслано (fallback)")
        except Exception as e:
            logger.warning(f"⚠️ Не вдалося надіслати тренеру @{trainer_username}: {e}")

    # Повідомлення адміністратору з кнопками підтвердження
    user_tg   = f"@{call.from_user.username}" if call.from_user.username else f"ID {cid}"
    admin_msg = (
        f"📋 *Новий запис до тренера\\!*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"👤 *{data['name']}* \\({user_tg}\\)\n"
        f"📱 `{data['phone']}`\n"
        f"🧒 {data.get('age_type','—')}  │  📚 {data.get('lesson_type','—')}\n"
        f"♟️ `{level_badge}`\n\n"
        f"👨\u200d🏫 Тренер: *{trainer_name}* \\(@{trainer_username}\\)\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"Підтвердіть або відхиліть запис:"
    )
    confirm_markup = types.InlineKeyboardMarkup()
    confirm_markup.row(
        types.InlineKeyboardButton("✅ Підтвердити", callback_data=f"confirm_enroll_{cid}_{tid}"),
        types.InlineKeyboardButton("❌ Відхилити",   callback_data=f"reject_enroll_{cid}_{tid}")
    )
    try:
        bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown", reply_markup=confirm_markup)
    except Exception as e:
        logger.error(f"❌ Не вдалося надіслати адміну: {e}")


# ==========================================================
# ✅  АДМІН ПІДТВЕРДЖУЄ ЗАПИС
# ==========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_enroll_"))
def admin_confirm_enroll(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Тільки для адміна", show_alert=True)
        return

    parts = call.data.split("_")
    try:
        user_cid = int(parts[2])
        tid      = int(parts[3])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    # ВИПРАВЛЕННЯ №2: перевірка наявності даних до обробки
    data = user_form.get(user_cid)
    if not data:
        bot.answer_callback_query(call.id, "❌ Дані заявки не знайдені", show_alert=True)
        return

    trainer_name     = data.get("trainer_name", "тренера")
    trainer_username = data.get("trainer_username")
    trainer_tg_id    = data.get("trainer_tg_id")
    user_name        = data.get("name", "учня")
    user_phone       = data.get("phone", "—")
    user_level       = data.get("level", "—")
    user_level_badge = LEVEL_BADGE.get(user_level, user_level)
    age_type         = data.get("age_type", "—")
    lesson_type      = data.get("lesson_type", "—")

    bot.answer_callback_query(call.id, "✅ Запис підтверджено!")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.edit_message_text(
            call.message.text + "\n\n✅ _Підтверджено адміністратором._",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )
    except Exception:
        pass

    # Повідомлення учню
    try:
        bot.send_message(
            user_cid,
            f"🎉 *Запис підтверджено\\!*\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"👨\u200d🏫 Тренер: *{trainer_name}*\n\n"
            f"⏳ Адміністратор незабаром зв'яжеться з вами\n"
            f"для узгодження деталей занять\\.\n\n"
            f"_Дякуємо за довіру\\! Бажаємо успіхів\\! ♟️_",
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.warning(f"Не вдалося надіслати учню {user_cid}: {e}")

    # Повідомлення тренеру
    confirm_trainer_msg = (
        f"✅ *Адмін підтвердив запис\\!*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"До вас записався новий учень:\n"
        f"👤 *{user_name}*\n"
        f"📱 `{user_phone}`\n"
        f"🧒 {age_type}  │  📚 {lesson_type}\n"
        f"♟️ `{user_level_badge}`\n\n"
        f"_Зв'яжіться з учнем для організації занять\\!_"
    )
    trainer_notified = False
    if trainer_tg_id:
        try:
            bot.send_message(int(trainer_tg_id), confirm_trainer_msg, parse_mode="MarkdownV2")
            trainer_notified = True
        except Exception as e:
            logger.warning(f"Не вдалося надіслати тренеру ID={trainer_tg_id}: {e}")
    if not trainer_notified and trainer_username:
        try:
            bot.send_message(
                f"@{trainer_username}", confirm_trainer_msg, parse_mode="MarkdownV2"
            )
        except Exception as e:
            logger.warning(f"Не вдалося надіслати тренеру @{trainer_username}: {e}")

    # Очищаємо сесію після завершення
    user_form.pop(user_cid, None)
    user_states.pop(user_cid, None)


# ==========================================================
# ❌  АДМІН ВІДХИЛЯЄ ЗАПИС → просимо причину
# ==========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("reject_enroll_"))
def admin_reject_enroll(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Тільки для адміна", show_alert=True)
        return

    parts = call.data.split("_")
    try:
        user_cid = int(parts[2])
        tid      = int(parts[3])
    except Exception:
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    data         = user_form.get(user_cid)
    trainer_name = data.get("trainer_name", "") if data else ""

    # Зберігаємо стан і дані для хендлера причини
    user_states[ADMIN_ID]    = "waiting_reject_reason"
    trainer_form[ADMIN_ID]   = {
        "user_cid":     user_cid,
        "trainer_name": trainer_name,
        "tid":          tid
    }

    bot.answer_callback_query(call.id)

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.edit_message_text(
            call.message.text + "\n\n⏳ Очікуємо причину від адміністратора...",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )
    except Exception:
        pass

    bot.send_message(
        call.message.chat.id,
        "❌ *Відхилення запису*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "Напишіть причину відмови для учня:",
        parse_mode="Markdown",
        reply_markup=cancel_only_markup()
    )


# ==========================================================
# ✏️  АДМІН ВВОДИТЬ ПРИЧИНУ ВІДХИЛЕННЯ
# ВИПРАВЛЕННЯ №3: доданий відсутній хендлер
# ==========================================================
@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "waiting_reject_reason")
def admin_write_reject_reason(message):
    if message.from_user.id != ADMIN_ID:
        return

    data = trainer_form.get(message.chat.id)
    if not data:
        bot.send_message(
            message.chat.id,
            "❌ Помилка: дані не знайдені.",
            reply_markup=admin_menu_markup()
        )
        user_states.pop(message.chat.id, None)
        return

    reason       = message.text.strip() if message.text else "Без причини"
    user_cid     = data["user_cid"]
    trainer_name = data.get("trainer_name", "тренера")

    # Надсилаємо учню повідомлення з причиною
    try:
        bot.send_message(
            user_cid,
            f"😔 *Запис відхилено*\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"👨\u200d🏫 Тренер: *{trainer_name}*\n\n"
            f"📝 *Причина:*\n{reason}\n\n"
            f"_Спробуйте обрати іншого тренера або зверніться до адміністратора\\._",
            parse_mode="MarkdownV2",
            reply_markup=main_menu_markup(user_cid)
        )
    except Exception as e:
        logger.warning(f"Не вдалося надіслати учню {user_cid}: {e}")

    bot.send_message(
        message.chat.id,
        "✅ Учня повідомлено про відмову.",
        reply_markup=admin_menu_markup()
    )

    # Очищення всіх сесій
    user_states.pop(message.chat.id, None)
    trainer_form.pop(message.chat.id, None)
    user_form.pop(user_cid, None)
    user_states.pop(user_cid, None)


# ==========================================================
# 💬  ЧАТ З АДМІНІСТРАТОРОМ
# ==========================================================
@bot.message_handler(func=lambda m: m.text == "💬 Зв'язатися з адміністратором")
def contact_admin_start(message):
    user_states[message.chat.id] = "waiting_admin_response"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(BTN_CANCEL)
    bot.send_message(
        message.chat.id,
        "💬 *Зв'язок з адміністратором*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "⏳ Запит надіслано\\. Очікуйте відповіді\\.\n\n"
        "_Натисніть_ ❌ _Скасувати, щоб повернутись до меню_",
        parse_mode="MarkdownV2",
        reply_markup=markup
    )

    user_info    = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.chat.id}"
    admin_markup = types.InlineKeyboardMarkup()
    admin_markup.add(
        types.InlineKeyboardButton("✅ Прийняти",  callback_data=f"chat_accept_{message.chat.id}"),
        types.InlineKeyboardButton("❌ Відхилити", callback_data=f"chat_reject_{message.chat.id}")
    )
    bot.send_message(
        ADMIN_ID,
        f"📞 Запит на чат від {user_info}\nІм'я: {message.from_user.first_name}",
        reply_markup=admin_markup
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("chat_accept_"))
def chat_accept(call):
    uid = int(call.data.split("_")[2])
    if uid in admin_chats:
        bot.answer_callback_query(call.id, "⚠️ Цей чат вже активний", show_alert=True)
        return
    admin_chats[uid] = call.from_user.id
    user_states[uid] = "in_admin_chat"
    bot.edit_message_text("✅ Чат прийнято.", call.message.chat.id, call.message.message_id)

    admin_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    admin_kb.add(BTN_CANCEL)
    bot.send_message(
        call.from_user.id,
        f"💬 *Чат розпочато*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"Спілкуєтесь з користувачем `{uid}`\n\n"
        f"_Натисніть_ ❌ _Скасувати, щоб завершити чат_",
        parse_mode="Markdown",
        reply_markup=admin_kb
    )
    user_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    user_kb.add(BTN_CANCEL)
    bot.send_message(
        uid,
        "✅ *Адміністратор на зв'язку\\!*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "Пишіть ваше запитання\\.\n\n"
        "_Натисніть_ ❌ _Скасувати, щоб завершити чат_",
        parse_mode="MarkdownV2",
        reply_markup=user_kb
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("chat_reject_"))
def chat_reject(call):
    uid = int(call.data.split("_")[2])
    bot.edit_message_text("❌ Відхилено.", call.message.chat.id, call.message.message_id)
    user_states.pop(uid, None)
    bot.send_message(
        uid,
        "😔 *Адміністратор зараз недоступний*\n\n"
        "_Спробуйте пізніше або оберіть іншу дію_",
        parse_mode="Markdown",
        reply_markup=main_menu_markup(uid)
    )


@bot.message_handler(
    func=lambda m: user_states.get(m.chat.id) == "in_admin_chat"
                   and m.chat.id in admin_chats
)
def relay_user_to_admin(message):
    admin_id = admin_chats.get(message.chat.id)
    if admin_id:
        try:
            bot.send_message(admin_id, f"👤 Користувач: {message.text}")
        except Exception as e:
            logger.warning(f"relay_user→admin: {e}")


@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID
                   and any(a == ADMIN_ID for a in admin_chats.values())
                   and m.text not in ADMIN_RESERVED
)
def relay_admin_to_user(message):
    user_id = next((u for u, a in admin_chats.items() if a == message.from_user.id), None)
    if not user_id:
        bot.send_message(message.chat.id, "ℹ️ Активних чатів немає.")
        return
    try:
        bot.send_message(user_id, f"👨‍💼 Адмін: {message.text}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Не вдалося надіслати: {e}")


# ==========================================================
# 🌐  FLASK ENDPOINTS
# ==========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = telebot.types.Update.de_json(request.get_json())
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"webhook: {e}")
    return "", 200


@app.route("/health", methods=["GET"])
def health():
    db = get_db()
    return ("OK", 200) if db else ("ERROR", 500)


@app.route("/debug", methods=["GET"])
def debug_db():
    try:
        payload = {"requests": [
            {"type": "execute", "stmt": {"sql": "SELECT id, name, username, description FROM trainers"}},
            {"type": "close"}
        ]}
        raw  = requests.post(
            f"{TURSO_URL}/v2/pipeline", json=payload,
            headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"},
            timeout=10
        )
        db   = get_db()
        rows = []
        if db:
            try:
                rows = [list(r) for r in db.execute(
                    "SELECT id, name, username, description FROM trainers"
                ).rows]
            except Exception as e:
                rows = [f"ERROR: {e}"]
        return (
            json.dumps(
                {"http_status": raw.status_code,
                 "raw_turso_response": raw.json(),
                 "parsed_rows": rows},
                ensure_ascii=False, indent=2
            ),
            200,
            {"Content-Type": "application/json"}
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False), 500


# ==========================================================
# 🚀  ЗАПУСК
# ==========================================================
if __name__ == "__main__":
    logger.info("🚀 Запуск бота…")
    if not init_db():
        logger.error("❌ Не вдалося ініціалізувати БД")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info(f"✅ Webhook встановлено: {WEBHOOK_URL}/webhook")
    except Exception as e:
        logger.error(f"❌ set_webhook: {e}")
    port = int(os.getenv("PORT", 5000))
    logger.info(f"🌐 Порт {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
