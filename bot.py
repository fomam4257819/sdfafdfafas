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
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "ТВІЙ_ТОКЕН_БОТА")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "887078537"))
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "https://78655.onrender.com")
TURSO_URL    = os.getenv("TURSO_URL",   "https://1qaz2wsx-yhbvgt65.aws-eu-west-1.turso.io")
TURSO_TOKEN  = os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9...")

MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 2

# =========================
# 📊 СТАНИ СЕСІЙ (в пам'яті)
# =========================
user_states  = {}   # {chat_id: "state_name"}
user_form    = {}   # {chat_id: {phone, name, level}}  — тимчасово, не зберігається в БД
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

        payload  = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
        resp     = requests.post(f"{self.url}/v2/pipeline", json=payload, headers=self.headers, timeout=10)
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
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("✅ Таблиця trainers готова")
        return True
    except Exception as e:
        logger.error(f"❌ init_db: {e}")
        return False

# ==========================================================
# 🛠️  ДОПОМІЖНІ ФУНКЦІЇ
# ==========================================================

def main_menu_markup(user_id: int) -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("♟️ Вибрати тренера", "💬 Зв'язатися з адміністратором")
    if user_id == ADMIN_ID:
        m.add("⚙️ Edit")
    return m


def admin_menu_markup() -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("➕ Додати тренера", "➖ Видалити тренера")
    m.add("📋 Список тренерів")
    m.add("⬅️ Назад")
    return m


def send_main_menu(chat_id, user_id, text="Головне меню:"):
    user_states[chat_id] = "main_menu"
    bot.send_message(chat_id, text, reply_markup=main_menu_markup(user_id))


def _cancel_user_flow(message):
    user_states.pop(message.chat.id, None)
    user_form.pop(message.chat.id, None)
    send_main_menu(message.chat.id, message.from_user.id, "Скасовано.")

# ==========================================================
# 🏁  /start
# ==========================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    send_main_menu(message.chat.id, message.from_user.id,
                   "♟️ Ласкаво просимо до Шахової школи!\nОберіть дію:")

# ==========================================================
# 👨‍💼  АДМІН-ПАНЕЛЬ
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "⚙️ Edit")
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Немає доступу.")
        return
    user_states[message.chat.id] = "admin_panel"
    bot.send_message(message.chat.id, "👨‍💼 Адмін-панель:", reply_markup=admin_menu_markup())


@bot.message_handler(func=lambda m: m.text == "⬅️ Назад")
def admin_back(message):
    send_main_menu(message.chat.id, message.from_user.id, "🔙 Повернення до меню.")


# ── ДОДАТИ ТРЕНЕРА ─────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "➕ Додати тренера")
def add_trainer_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    trainer_form[message.chat.id] = {}
    user_states[message.chat.id]  = "add_trainer_username"
    bot.send_message(message.chat.id,
                     "Введіть @username тренера у Telegram:\n(приклад: @chess_coach_ivan)")


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_username")
def add_trainer_username(message):
    username = message.text.strip()
    if not username.startswith("@"):
        bot.send_message(message.chat.id, "❌ Username має починатися з @. Спробуйте ще раз:")
        return
    trainer_form[message.chat.id].update({"username": username[1:], "display_username": username})
    user_states[message.chat.id] = "add_trainer_name"
    bot.send_message(message.chat.id, "Введіть повне ім'я тренера:")


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_name")
def add_trainer_name(message):
    trainer_form[message.chat.id]["name"] = message.text.strip()
    user_states[message.chat.id] = "add_trainer_description"
    bot.send_message(message.chat.id, "Введіть опис тренера (досвід, спеціалізація тощо):")


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "add_trainer_description")
def add_trainer_description(message):
    data = trainer_form.get(message.chat.id, {})
    data["description"] = message.text.strip()
    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        user_states.pop(message.chat.id, None)
        trainer_form.pop(message.chat.id, None)
        return
    try:
        db.execute("INSERT INTO trainers (username, name, description) VALUES (?, ?, ?)",
                   [data["username"], data["name"], data["description"]])
        bot.send_message(message.chat.id,
                         f"✅ Тренер *{data['name']}* ({data['display_username']}) успішно доданий!",
                         parse_mode="Markdown", reply_markup=admin_menu_markup())
        logger.info(f"✅ Додано тренера: {data['name']}")
    except Exception as e:
        if "unique" in str(e).lower() or "constraint" in str(e).lower():
            bot.send_message(message.chat.id,
                             f"⚠️ Тренер {data['display_username']} вже існує.",
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
        trainers = db.execute("SELECT id, name FROM trainers ORDER BY name").rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        return
    if not trainers:
        bot.send_message(message.chat.id, "📭 Тренерів немає.")
        return
    markup = types.InlineKeyboardMarkup()
    for row in trainers:
        markup.add(types.InlineKeyboardButton(f"❌ {row[1]}", callback_data=f"del_trainer_{row[0]}"))
    bot.send_message(message.chat.id, f"Оберіть тренера для видалення ({len(trainers)} чол.):",
                     reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("del_trainer_"))
def delete_trainer_confirm(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Немає доступу", show_alert=True)
        return
    tid = int(call.data.split("_")[2])
    db  = get_db()
    if not db:
        bot.answer_callback_query(call.id, "❌ Помилка БД", show_alert=True)
        return
    try:
        result = db.execute("SELECT name FROM trainers WHERE id = ?", [tid])
        if not result.rows:
            bot.answer_callback_query(call.id, "❌ Не знайдено", show_alert=True)
            return
        name = str(result.rows[0][0])
        db.execute("DELETE FROM trainers WHERE id = ?", [tid])
        bot.answer_callback_query(call.id, "✅ Видалено!")
        bot.edit_message_text(f"✅ Тренер *{name}* видалений.",
                              call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:50]}", show_alert=True)


# ── СПИСОК ТРЕНЕРІВ (адмін) ─────────────────────────────

@bot.message_handler(func=lambda m: m.text == "📋 Список тренерів")
def list_trainers(message):
    if message.from_user.id != ADMIN_ID:
        return
    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        return
    try:
        trainers = db.execute("SELECT id, name, username, description FROM trainers ORDER BY name").rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        return
    if not trainers:
        bot.send_message(message.chat.id, "📭 Список тренерів порожній.")
        return
    lines = [f"📋 *Тренери* ({len(trainers)} чол.):\n"]
    for i, row in enumerate(trainers, 1):
        name  = str(row[1])
        uname = str(row[2])
        desc  = str(row[3]) if row[3] else "—"
        lines.append(f"{i}. *{name}* (@{uname})\n_{desc}_\n")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")

# ==========================================================
# 👤  ВИБІР ТРЕНЕРА (користувач)
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "♟️ Вибрати тренера")
def choose_trainer_start(message):
    user_states[message.chat.id] = "user_waiting_phone"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📱 Поділитися номером", request_contact=True))
    markup.add("⬅️ Скасувати")
    bot.send_message(message.chat.id, "Поділіться вашим номером телефону:", reply_markup=markup)


@bot.message_handler(content_types=["contact"])
def user_got_phone(message):
    if user_states.get(message.chat.id) != "user_waiting_phone":
        return
    user_form[message.chat.id] = {"phone": message.contact.phone_number}
    user_states[message.chat.id] = "user_waiting_name"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("⬅️ Скасувати")
    bot.send_message(message.chat.id, "Введіть ваше ім'я:", reply_markup=markup)


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_name")
def user_got_name(message):
    if message.text == "⬅️ Скасувати":
        _cancel_user_flow(message)
        return
    user_form[message.chat.id]["name"] = message.text.strip()
    user_states[message.chat.id] = "user_waiting_level"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🌱 Початківець", "🎯 Аматор")
    markup.row("⚔️ Просунутий",  "👑 Експерт")
    markup.add("⬅️ Скасувати")
    bot.send_message(message.chat.id, "Оберіть ваш рівень гри:", reply_markup=markup)


@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "user_waiting_level")
def user_got_level(message):
    if message.text == "⬅️ Скасувати":
        _cancel_user_flow(message)
        return
    allowed = {"🌱 Початківець", "🎯 Аматор", "⚔️ Просунутий", "👑 Експерт"}
    if message.text not in allowed:
        bot.send_message(message.chat.id, "Оберіть рівень із кнопок нижче.")
        return
    user_form[message.chat.id]["level"] = message.text

    db = get_db()
    if not db:
        bot.send_message(message.chat.id, "❌ Помилка підключення до БД.")
        _cancel_user_flow(message)
        return
    try:
        trainers = db.execute("SELECT id, name, description FROM trainers ORDER BY name").rows
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        _cancel_user_flow(message)
        return

    if not trainers:
        bot.send_message(message.chat.id, "😔 Тренерів ще немає. Спробуйте пізніше.",
                         reply_markup=main_menu_markup(message.from_user.id))
        user_states.pop(message.chat.id, None)
        user_form.pop(message.chat.id, None)
        return

    markup = types.InlineKeyboardMarkup()
    for row in trainers:
        tid   = row[0]
        name  = str(row[1])
        desc  = str(row[2]) if row[2] else ""
        label = f"👨‍🏫 {name}" + (f"  •  {desc[:28]}…" if len(desc) > 28 else (f"  •  {desc}" if desc else ""))
        markup.add(types.InlineKeyboardButton(label, callback_data=f"pick_trainer_{tid}"))

    bot.send_message(message.chat.id, f"Доступні тренери ({len(trainers)} чол.):\nОберіть тренера:",
                     reply_markup=markup)
    user_states[message.chat.id] = "user_picking_trainer"


@bot.callback_query_handler(func=lambda c: c.data.startswith("pick_trainer_"))
def user_picked_trainer(call):
    tid  = int(call.data.split("_")[2])
    data = user_form.get(call.message.chat.id)
    if not data:
        bot.answer_callback_query(call.id, "❌ Сесія застаріла. Натисніть /start", show_alert=True)
        return
    db = get_db()
    if not db:
        bot.answer_callback_query(call.id, "❌ Помилка БД", show_alert=True)
        return
    try:
        result  = db.execute("SELECT username, name FROM trainers WHERE id = ?", [tid])
        trainer = result.rows[0] if result.rows else None
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {str(e)[:50]}", show_alert=True)
        return
    if not trainer:
        bot.answer_callback_query(call.id, "❌ Тренера не знайдено", show_alert=True)
        return

    username     = str(trainer[0])
    trainer_name = str(trainer[1])
    notification = (
        f"🎯 *Нова заявка!*\n\n"
        f"👤 Ім'я:    {data['name']}\n"
        f"📱 Телефон: {data['phone']}\n"
        f"♟️ Рівень:  {data['level']}"
    )
    try:
        bot.send_message(f"@{username}", notification, parse_mode="Markdown")
        bot.answer_callback_query(call.id, "✅ Заявку надіслано!")
        logger.info(f"📨 Заявку надіслано @{username}")
    except Exception as e:
        logger.warning(f"⚠️ Не вдалося надіслати @{username}: {e}")
        bot.answer_callback_query(call.id, "⚠️ Помилка надсилання (тренер не запустив бота?)", show_alert=True)

    bot.edit_message_text(
        f"✅ Заявку надіслано тренеру *{trainer_name}*!\nВін зв'яжеться з вами найближчим часом.",
        call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    send_main_menu(call.message.chat.id, call.from_user.id, "Що далі?")
    user_states.pop(call.message.chat.id, None)
    user_form.pop(call.message.chat.id, None)

# ==========================================================
# 💬  ЧАТ З АДМІНІСТРАТОРОМ
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "💬 Зв'язатися з адміністратором")
def contact_admin_start(message):
    user_states[message.chat.id] = "waiting_admin_response"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🛑 Завершити чат")
    bot.send_message(message.chat.id, "⏳ Запит надіслано адміністратору. Очікуйте…", reply_markup=markup)

    user_info    = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.chat.id}"
    admin_markup = types.InlineKeyboardMarkup()
    admin_markup.add(
        types.InlineKeyboardButton("✅ Прийняти",  callback_data=f"chat_accept_{message.chat.id}"),
        types.InlineKeyboardButton("❌ Відхилити", callback_data=f"chat_reject_{message.chat.id}")
    )
    bot.send_message(ADMIN_ID,
                     f"📞 Запит на чат від {user_info}\nІм'я: {message.from_user.first_name}",
                     reply_markup=admin_markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("chat_accept_"))
def chat_accept(call):
    uid = int(call.data.split("_")[2])
    if uid in admin_chats:
        bot.answer_callback_query(call.id, "⚠️ Цей чат вже активний", show_alert=True)
        return
    admin_chats[uid]     = call.from_user.id
    user_states[uid]     = "in_admin_chat"
    bot.edit_message_text("✅ Чат прийнято.", call.message.chat.id, call.message.message_id)

    admin_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    admin_kb.add("🛑 Завершити чат")
    bot.send_message(call.from_user.id, f"💬 Чат з користувачем {uid} розпочато.", reply_markup=admin_kb)

    user_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    user_kb.add("🛑 Завершити чат")
    bot.send_message(uid, "✅ Адміністратор прийняв запит. Пишіть!", reply_markup=user_kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("chat_reject_"))
def chat_reject(call):
    uid = int(call.data.split("_")[2])
    bot.edit_message_text("❌ Відхилено.", call.message.chat.id, call.message.message_id)
    user_states.pop(uid, None)
    bot.send_message(uid, "😔 Адміністратор зараз недоступний. Спробуйте пізніше.",
                     reply_markup=main_menu_markup(uid))


@bot.message_handler(func=lambda m: m.text == "🛑 Завершити чат")
def end_chat(message):
    cid = message.chat.id
    uid = message.from_user.id
    if uid == ADMIN_ID:
        user_id = next((u for u, a in admin_chats.items() if a == uid), None)
        if user_id:
            admin_chats.pop(user_id, None)
            user_states.pop(user_id, None)
            try:
                bot.send_message(user_id, "👋 Адміністратор завершив чат.",
                                 reply_markup=main_menu_markup(user_id))
            except Exception:
                pass
    else:
        if cid in admin_chats:
            admin_id = admin_chats.pop(cid)
            try:
                bot.send_message(admin_id, f"👤 Користувач завершив чат.")
            except Exception:
                pass
    user_states.pop(cid, None)
    send_main_menu(cid, uid, "👋 Чат завершено.")


@bot.message_handler(
    func=lambda m: user_states.get(m.chat.id) == "in_admin_chat" and m.chat.id in admin_chats
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
                   and m.text not in ("⚙️ Edit", "➕ Додати тренера", "➖ Видалити тренера",
                                      "📋 Список тренерів", "⬅️ Назад", "🛑 Завершити чат")
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
        raw  = requests.post(f"{TURSO_URL}/v2/pipeline", json=payload,
                             headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"},
                             timeout=10)
        db   = get_db()
        rows = []
        if db:
            try:
                rows = [list(r) for r in db.execute("SELECT id, name, username, description FROM trainers").rows]
            except Exception as e:
                rows = [f"ERROR: {e}"]
        return (json.dumps({"http_status": raw.status_code, "raw_turso_response": raw.json(),
                            "parsed_rows": rows}, ensure_ascii=False, indent=2),
                200, {"Content-Type": "application/json"})
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
