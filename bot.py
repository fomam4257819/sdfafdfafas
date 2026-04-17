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
TURSO_URL   = os.getenv("TURSO_URL",   "https://1qaz2wsx-yhbvgt65.aws-eu-west-1.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJleHAiOjE4MDc4NjA1NDEsImlhdCI6MTc3NjMyNDU0MSwiaWQiOiIwMTlkOTUyZC03YjAxLTc3N2QtYjE4NS03MDEzY2JjOWYwMDkiLCJyaWQiOiI3NmJlZDlhMy01Zjk1LTQ0OGYtYThkYi1kZTY2OTNmNjcwZTAifQ.fN9MZ5inviHOnUNqhrW20hbt1oUmHS6E2auA_grZ6pcv02NvEKEmrI5Ms_oSnwbBM1nTsR-TmE7SSIrB4utKDw")
MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 2

# =========================
# 📊 СТАНИ С��СІЙ (в пам'яті)
# =========================
user_states  = {}   # {chat_id: "state_name"}
user_form    = {}   # {chat_id: {phone, name, level, trainer_id, trainer_name, trainer_username}}
trainer_form = {}   # {chat_id: {username, name, description}}
admin_chats  = {}   # {user_chat_id: admin_chat_id}

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

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
        logger.info("✅ Підключено д�� Turso")
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
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
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


def send_main_menu(chat_id: int, user_id: int, text: str = "Головне меню:"):
    user_states.pop(chat_id, None)
    user_form.pop(chat_id, None)
    trainer_form.pop(chat_id, None)
    user_states[chat_id] = "main_menu"
    bot.send_message(chat_id, text, reply_markup=main_menu_markup(user_id))


def reset_to_main(message):
    """Универсальная отмена — всегда возвращает в главное меню.
    Если был активен чат с админом — корректно завершает его."""
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
            bot.send_message(admin_id, f"👤 Користувач завершив чат.")
        except Exception:
            pass

    send_main_menu(cid, uid, "↩️ Повернення до головного меню.")


def format_trainer_card(name: str, username: str, desc: str, index: int = None) -> str:
    """Красиво форматирует карточку тренера."""
    prefix = f"{index}\\. " if index else ""
    desc_text = desc if desc else "Опис не вказано"
    return (
        f"{prefix}👨‍🏫 *{name}*\n"
        f"📎 @{username}\n"
        f"📝 _{desc_text}_\n"
        f"{'─' * 28}"
    )

# ==========================================================
# 🏁  /start
# ==========================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    send_main_menu(message.chat.id, message.from_user.id,
                   "♟️ Ласкаво просимо до Шахової школи!\nОберіть дію:")


# ==========================================================
# ❌  УНИВЕРСАЛЬНАЯ ОТМЕНА  (регистрируется первой!)
# ==========================================================
@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def universal_cancel(message):
    reset_to_main(message)


# ==========================================================
# 👨‍🏫  НАШІ ТРЕНЕРИ (публичная кнопка в главном меню)
# ==========================================================
@bot.message_handler(func=lambda m: m.text == "👨‍🏫 Наші тренери")
def show_our_trainers(message):
    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute(
            "SELECT id, name, username, description FROM trainers ORDER BY name"
        ).rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        return

    if not trainers:
        bot.send_message(
            message.chat.id,
            "😔 Тренерів поки немає. Перевірте пізніше!",
            reply_markup=main_menu_markup(message.from_user.id)
        )
        return

    header = f"👨‍🏫 *Наші тренери* — {len(trainers)} фахівці:\n\n"
    bot.send_message(message.chat.id, header, parse_mode="Markdown")

    for i, row in enumerate(trainers, 1):
        # Рaспаковка значений Turso (формат {type, value}) перед приведением к str
        name  = _unpack_turso_value(row[1])
        uname = _unpack_turso_value(row[2])
        desc  = _unpack_turso_value(row[3])

        name  = str(name)
        uname = str(uname)
        desc  = str(desc) if desc else "Опис не вказано"

        card  = format_trainer_card(name, uname, desc, index=i)
        bot.send_message(message.chat.id, card, parse_mode="Markdown")

    bot.send_message(
        message.chat.id,
        "Хочете записатись? Натисніть *♟️ Вибрати тренера*",
        parse_mode="Markdown",
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
    user_states[message.chat.id] = "admin_panel"
    bot.send_message(message.chat.id, "👨‍💼 Адмін-панель:", reply_markup=admin_menu_markup())


@bot.message_handler(func=lambda m: m.text == BTN_BACK)
def admin_back(message):
    send_main_menu(message.chat.id, message.from_user.id, "🔙 Повернення до меню.")


# ── ДОДАТИ ТРЕНЕРА ─────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "➕ Додати тренера")
def add_trainer_start(message):
    if message.from_user.id != ADMIN_ID:
        return

    trainer_form[message.chat.id] = {}
    user_states[message.chat.id]  = "add_trainer_username"

    bot.send_message(
        message.chat.id,
        "📝 Додавання тренера — крок 1/3\n\n"
        "Введіть @username тренера\n\n"
        "Приклад:\n"
        "@chess_coach_ivan",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_username")
def add_trainer_username(message):
    username = message.text.strip()
    if not username.startswith("@"):
        bot.send_message(message.chat.id, "❌ Username має починатися з @. Спробуйте ще раз:")
        return
    trainer_form[message.chat.id].update({"username": username[1:], "display_username": username})
    user_states[message.chat.id] = "add_trainer_name"
    bot.send_message(
        message.chat.id,
        "📝 *Додавання тренера* — крок 2/3\n\nВведіть повне ім'я тренера:",
        parse_mode="Markdown",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_name")
def add_trainer_name(message):
    trainer_form[message.chat.id]["name"] = message.text.strip()
    user_states[message.chat.id] = "add_trainer_description"
    bot.send_message(
        message.chat.id,
        "📝 *Додавання тренера* — крок 3/3\n\n"
        "Введіть опис тренера _(досвід, звання, спеціалізація)_:",
        parse_mode="Markdown",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_description")
def add_trainer_description(message):
    data = trainer_form.get(message.chat.id, {})
    data["description"] = message.text.strip()
    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.", reply_markup=admin_menu_markup())
        user_states.pop(message.chat.id, None)
        trainer_form.pop(message.chat.id, None)
        return
    try:
        db.execute(
            "INSERT INTO trainers (username, name, description) VALUES (?, ?, ?)",
            [data["username"], data["name"], data["description"]]
        )
        bot.send_message(
            message.chat.id,
            f"✅ Тренер *{data['name']}* ({data['display_username']}) успішно доданий!",
            parse_mode="Markdown",
            reply_markup=admin_menu_markup()
        )
        logger.info(f"✅ Додано тренера: {data['name']}")
    except Exception as e:
        if "unique" in str(e).lower() or "constraint" in str(e).lower():
            bot.send_message(message.chat.id,
                             f"⚠️ Тренер {data['display_username']} вже існує в базі.",
                             reply_markup=admin_menu_markup())
        else:
            bot.send_message(message.chat.id, f"❌ Помилка: {e}", reply_markup=admin_menu_markup())
    user_states.pop(message.chat.id, None)
    trainer_form.pop(message.chat.id, None)


# ── ВИДАЛИТИ ТРЕНЕРА ────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "➖ Видалити тренера")
def delete_trainer_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute("SELECT id, name, username FROM trainers ORDER BY name").rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        return
    if not trainers:
        bot.send_message(message.chat.id, "📭 Тренерів немає.", reply_markup=admin_menu_markup())
        return

    # Отправляем каждого тренера отдельным сообщением с кнопкой "🗑 Вилучити"
    bot.send_message(message.chat.id, f"🗑 Оберіть тренера для видалення ({len(trainers)} чол.):\n⚠️ Видалення відбудеться одразу.")
    for row in trainers:
        # Распаковка значений Turso
        tid_raw = _unpack_turso_value(row[0])
        try:
            tid = int(tid_raw)
        except Exception:
            # если не удалось привести к int — пропускаем
            continue
        name  = _unpack_turso_value(row[1])
        uname = _unpack_turso_value(row[2])

        name  = str(name) if name is not None else "Без імені"
        uname = str(uname) if uname is not None else "no_username"

        card = (
            f"👨‍🏫 {name}\n"
            f"📎 @{uname}\n"
        )

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
        result = db.execute("SELECT name FROM trainers WHERE id = ?", [tid])
        if not result.rows:
            bot.answer_callback_query(call.id, "❌ Тренера не знайдено", show_alert=True)
            return
        # распаковка результата на всякий случай
        name_raw = result.rows[0][0]
        name = _unpack_turso_value(name_raw) if isinstance(name_raw, dict) else name_raw
        name = str(name)

        db.execute("DELETE FROM trainers WHERE id = ?", [tid])
        logger.info(f"🗑 Видалено тренера: {name} (id={tid})")

        bot.answer_callback_query(call.id, f"✅ {name} видалений!")
        bot.edit_message_text(
            f"✅ Тренер *{name}* успішно видалений з бази.",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )
        bot.send_message(call.message.chat.id, "Що далі?", reply_markup=admin_menu_markup())

    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:80]}", show_alert=True)


# ── СПИСОК ТРЕНЕРІВ (адмін) — красивые карточки ─────────
@bot.message_handler(func=lambda m: m.text == "📋 Список тренерів")
def list_trainers(message):
    if message.from_user.id != ADMIN_ID:
        return
    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute(
            "SELECT id, name, username, description FROM trainers ORDER BY name"
        ).rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        return
    if not trainers:
        bot.send_message(message.chat.id, "📭 Список тренерів порожній.")
        return

    bot.send_message(
        message.chat.id,
        f"📋 *Тренери в базі* — {len(trainers)} чол.:",
        parse_mode="Markdown"
    )
    for i, row in enumerate(trainers, 1):
        tid   = int(row[0])
        name  = str(row[1])
        uname = str(row[2])
        desc  = str(row[3]) if row[3] else ""

        card = (
            f"*{i}\\. {name}*\n"
            f"🔗 @{uname}  \\(ID: `{tid}`\\)\n"
            f"📝 _{desc if desc else 'Опис не вказано'}_"
        )
        bot.send_message(message.chat.id, card, parse_mode="MarkdownV2")


# ==========================================================
# 👤  ВИБІР ТРЕНЕРА (пользователь)
# ==========================================================
@bot.message_handler(func=lambda m: m.text == "♟️ Вибрати тренера")
def choose_trainer_start(message):
    user_states[message.chat.id] = "user_waiting_phone"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📱 Поділитися номером", request_contact=True))
    markup.add(BTN_CANCEL)
    bot.send_message(
        message.chat.id,
        "📱 *Крок 1 з 3* — Поділіться вашим номером телефону\n\n"
        "Натисніть кнопку нижче — це потрібно для зв'язку з тренером:",
        parse_mode="Markdown",
        reply_markup=markup
    )


@bot.message_handler(content_types=["contact"])
def user_got_phone(message):
    if user_states.get(message.chat.id) != "user_waiting_phone":
        return
    user_form[message.chat.id] = {"phone": message.contact.phone_number}
    user_states[message.chat.id] = "user_waiting_name"
    bot.send_message(
        message.chat.id,
        "✍️ *Крок 2 з 3* — Введіть ваше ім'я та прізвище:",
        parse_mode="Markdown",
        reply_markup=cancel_only_markup()
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_name")
def user_got_name(message):
    user_form[message.chat.id]["name"] = message.text.strip()
    user_states[message.chat.id] = "user_waiting_level"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🌱 Початківець", "🎯 Аматор")
    markup.row("⚔️ Просунутий",  "👑 Експерт")
    markup.add(BTN_CANCEL)
    bot.send_message(
        message.chat.id,
        "♟️ *Крок 3 з 3* — Оберіть ваш рівень гри:",
        parse_mode="Markdown",
        reply_markup=markup
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_level")
def user_got_level(message):
    allowed = {"🌱 Початківець", "🎯 Аматор", "⚔️ Просунутий", "👑 Експерт"}
    if message.text not in allowed:
        bot.send_message(message.chat.id, "Оберіть рівень із кнопок нижче.")
        return
    user_form[message.chat.id]["level"] = message.text

    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        reset_to_main(message)
        return
    try:
        trainers = db.execute(
            "SELECT id, name, description FROM trainers ORDER BY name"
        ).rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        reset_to_main(message)
        return

    if not trainers:
        bot.send_message(
            message.chat.id,
            "😔 Тренерів поки немає. Спробуйте пізніше.",
            reply_markup=main_menu_markup(message.from_user.id)
        )
        user_states.pop(message.chat.id, None)
        user_form.pop(message.chat.id, None)
        return

    bot.send_message(
        message.chat.id,
        f"✅ Рівень *{message.text}* збережено\\!\n\n"
        f"👇 Оберіть тренера — натисніть кнопку під карткою:",
        parse_mode="MarkdownV2",
        reply_markup=cancel_only_markup()
    )

    # Исправленная раскладка значений Turso и новая кнопка
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
            f"👨‍🏫 *{name}*\n"
            f"─────────────────────\n"
            f"📝 {desc}"
        )
        pick_markup = types.InlineKeyboardMarkup()
        pick_markup.add(
            types.InlineKeyboardButton(
                "📚 Записатися на курс",
                callback_data=f"pick_{tid}"
            )
        )
        bot.send_message(
            message.chat.id,
            card,
            parse_mode="Markdown",
            reply_markup=pick_markup
        )

    user_states[message.chat.id] = "user_picking_trainer"


# ==========================================================
# ✅  ОБРАТИ ТРЕНЕРА → запрос на подтверждение админу
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
        # Получаем тренера из БД и распаковываем значения
        result = db.execute(
            "SELECT username, name FROM trainers WHERE id = ?",
            [tid]
        ).rows

        if not result:
            bot.answer_callback_query(call.id, "❌ Тренер не знайдений", show_alert=True)
            return

        trainer_username = _unpack_turso_value(result[0][0])
        trainer_name     = _unpack_turso_value(result[0][1])

        trainer_username = str(trainer_username)
        trainer_name     = str(trainer_name)

    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:50]}", show_alert=True)
        return

    # Сохраняем данные тренера в сессию — нужны при подтверждении
    data["trainer_id"]       = tid
    data["trainer_name"]     = trainer_name
    data["trainer_username"] = trainer_username

    bot.answer_callback_query(call.id, "📨 Заявку надіслано!")

    # --- Сообщение ученику ---
    bot.edit_message_text(
        f"✅ Ви записались до тренера *{trainer_name}*\\!\n\n"
        f"⏳ Очікуйте підтвердження адміністратора\\.\n"
        f"Як тільки адмін підтвердить — ви отримаєте повідомлення\\.",
        cid, call.message.message_id,
        parse_mode="MarkdownV2"
    )
    send_main_menu(cid, call.from_user.id, "Ми повідомимо вас!")

    # --- Сообщение тренеру (предварительное — без подтверждения) ---
    trainer_msg = (
        f"📬 *Новий запит на заняття\\!*\n\n"
        f"👤 Учень: *{data['name']}*\n"
        f"📱 Телефон: `{data['phone']}`\n"
        f"♟️ Рівень: {data['level']}\n\n"
        f"_Очікуйте підтвердження від адміністратора\\._"
    )
    try:
        bot.send_message(f"@{trainer_username}", trainer_msg, parse_mode="MarkdownV2")
        logger.info(f"📨 Попередження тренеру @{trainer_username} надіслано")
    except Exception as e:
        logger.warning(f"⚠️ Не вдалося надіслати тренеру @{trainer_username}: {e}")

    # --- Сообщение АДМИНИСТРАТОРУ с кнопкой подтверждения ---
    user_tg = f"@{call.from_user.username}" if call.from_user.username else f"ID {cid}"
    admin_msg = (
        f"📋 *Новий запис до тренера\\!*\n\n"
        f"👤 Учень: *{data['name']}* \\({user_tg}\\)\n"
        f"📱 Телефон: `{data['phone']}`\n"
        f"♟️ Рівень: {data['level']}\n"
        f"👨‍🏫 Тренер: *{trainer_name}* \\(@{trainer_username}\\)\n\n"
        f"Підтвердіть або відхиліть запис:"
    )
    confirm_markup = types.InlineKeyboardMarkup()
    confirm_markup.row(
        types.InlineKeyboardButton(
            "✅ Підтвердити",
            callback_data=f"confirm_enroll_{cid}_{tid}"
        ),
        types.InlineKeyboardButton(
            "❌ Відхилити",
            callback_data=f"reject_enroll_{cid}_{tid}"
        )
    )
    try:
        bot.send_message(ADMIN_ID, admin_msg, parse_mode="MarkdownV2", reply_markup=confirm_markup)
    except Exception as e:
        logger.error(f"❌ Не вдалося надіслати адміну: {e}")


# ==========================================================
# ✅ / ❌  АДМІН ПІДТВЕРДЖУЄ / ВІДХИЛЯЄ ЗАПИС
# ==========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_enroll_"))
def admin_confirm_enroll(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Тільки для адміна", show_alert=True)
        return

    # callback: confirm_enroll_{user_cid}_{trainer_id}
    parts = call.data.split("_")
    # ["confirm","enroll","cid","tid"]
    try:
        user_cid = int(parts[2])
        tid      = int(parts[3])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    data = user_form.get(user_cid)

    bot.answer_callback_query(call.id, "✅ Запис підтверджено!")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.edit_message_text(
        call.message.text + "\n\n✅ *Підтверджено адміністратором\\.*",
        call.message.chat.id, call.message.message_id,
        parse_mode="MarkdownV2"
    )

    trainer_name     = data["trainer_name"]     if data else "тренера"
    trainer_username = data["trainer_username"] if data else None
    user_name        = data["name"]             if data else "учня"
    user_phone       = data["phone"]            if data else "—"
    user_level       = data["level"]            if data else "—"

    # --- Сообщение ученику ---
    try:
        bot.send_message(
            user_cid,
            f"🎉 *Запис підтверджено\\!*\n\n"
            f"Адміністратор підтвердив ваш запис до тренера *{trainer_name}*\\.\n"
            f"Тренер зв'яжеться з вами найближчим часом\\!",
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.warning(f"Не вдалося надіслати учню {user_cid}: {e}")

    # --- Сообщение тренеру ---
    if trainer_username:
        try:
            bot.send_message(
                f"@{trainer_username}",
                f"✅ *Адмін підтвердив запис\\!*\n\n"
                f"До вас записався новий учень:\n"
                f"👤 *{user_name}*\n"
                f"📱 `{user_phone}`\n"
                f"♟️ Рівень: {user_level}\n\n"
                f"Зв'яжіться з учнем для організації занять\\!",
                parse_mode="MarkdownV2"
            )
        except Exception as e:
            logger.warning(f"Не вдалося надіслати тренеру @{trainer_username}: {e}")

    # Очищаем сессию
    user_form.pop(user_cid, None)
    user_states.pop(user_cid, None)


@bot.callback_query_handler(func=lambda c: c.data.startswith("reject_enroll_"))
def admin_reject_enroll(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Тільки для адміна", show_alert=True)
        return

    parts = call.data.split("_")
    # ["reject","enroll","cid","tid"]
    try:
        user_cid = int(parts[2])
        tid      = int(parts[3])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некоректні дані", show_alert=True)
        return

    data = user_form.get(user_cid)
    trainer_name = data["trainer_name"] if data else "тренера"

    bot.answer_callback_query(call.id, "❌ Запис відхилено.")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.edit_message_text(
        call.message.text + "\n\n❌ *Відхилено адміністратором\\.*",
        call.message.chat.id, call.message.message_id,
        parse_mode="MarkdownV2"
    )

    # --- Сообщение ученику ---
    try:
        bot.send_message(
            user_cid,
            f"😔 *На жаль, запис відхилено\\.*\n\n"
            f"Адміністратор не зміг підтвердити ваш запис до тренера *{trainer_name}*\\.\n"
            f"Спробуйте обрати іншого тренера або зверніться до адміністратора\\.",
            parse_mode="MarkdownV2",
            reply_markup=main_menu_markup(user_cid)
        )
    except Exception as e:
        logger.warning(f"Не вдалося надіслати учню {user_cid}: {e}")

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
        "⏳ Запит надіслано адміністратору\\. Очікуйте…\n"
        "Натисніть *❌ Скасувати*, щоб повернутись до меню\\.",
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
        f"💬 Чат з користувачем {uid} розпочато\\.\n"
        f"Натисніть *❌ Скасувати*, щоб завершити\\.",
        parse_mode="MarkdownV2",
        reply_markup=admin_kb
    )

    user_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    user_kb.add(BTN_CANCEL)
    bot.send_message(
        uid,
        "✅ Адміністратор прийняв запит\\. Пишіть\\!\n"
        "Натисніть *❌ Скасувати*, щоб завершити чат\\.",
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
        "😔 Адміністратор зараз недоступний\\. Спробуйте пізніше\\.",
        parse_mode="MarkdownV2",
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
