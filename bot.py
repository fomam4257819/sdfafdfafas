import telebot
from telebot import types
import os
import logging
from flask import Flask, request
import time
import requests
import json

# =========================
# 📝 ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =========================
# 🔐 НАЛАШТУВАННЯ
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ТВІЙ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "887078537"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://78655.onrender.com")

TURSO_URL = os.getenv("TURSO_URL", "https://1qaz2wsx-yhbvgt65.aws-eu-west-1.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJleHAiOjE4MDc4NjA1NDEsImlhdCI6MTc3NjMyNDU0MSwiaWQiOiIwMTlkOTUyZC03YjAxLTc3N2QtYjE4NS03MDEzY2JjOWYwMDkiLCJyaWQiOiI3NmJlZDlhMy01Zjk1LTQ0OGYtYThkYi1kZTY2OTNmNjcwZTAifQ.fN9MZ5inviHOnUNqhrW20hbt1oUmHS6E2auA_grZ6pcv02NvEKEmrI5Ms_oSnwbBM1nTsR-TmE7SSIrB4utKDw")

MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 2

# =========================
# 📊 СТАНИ КОРИСТУВАЧІВ
# =========================
user_states = {}
user_form = {}
trainer_data = {}
admin_chats = {}

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# =========================
# 🗄️ ПІДКЛЮЧЕННЯ ДО БД (TURSO)
# =========================

def _unpack_turso_value(v):
    """Розпакувати значення Turso: {"type": "integer", "value": "5"} → 5"""
    if isinstance(v, dict):
        t = v.get("type", "")
        val = v.get("value")
        if val is None or t == "null":
            return None
        if t == "integer":
            try:
                return int(val)
            except:
                return val
        if t == "float":
            try:
                return float(val)
            except:
                return val
        return val  # text, blob — повертаємо як є
    return v  # вже просте значення

class QueryResult:
    """Результат запроса к БД"""
    def __init__(self, rows=None):
        self.rows = []
        
        if rows is None:
            logger.debug(f"QueryResult: rows is None")
            return
        
        if isinstance(rows, list):
            if len(rows) == 0:
                logger.debug(f"QueryResult: empty list")
                return
            
            first_row = rows[0]
            logger.debug(f"first_row type: {type(first_row)}, value: {str(first_row)[:300]}")
            
            if isinstance(first_row, dict):
                if "values" in first_row:
                    # Структура Turso — кожна клітинка може бути {"type":..., "value":...}
                    try:
                        self.rows = [
                            tuple(_unpack_turso_value(v) for v in row.get("values", []))
                            for row in rows
                        ]
                        logger.info(f"📦 Turso format parsed: {len(self.rows)} rows")
                        logger.debug(f"Rows: {self.rows}")
                    except Exception as e:
                        logger.error(f"❌ Error parsing Turso format: {e}")
                        self.rows = []
                else:
                    logger.warning(f"⚠️ Dict but no 'values' key: {list(first_row.keys())}")
                    try:
                        self.rows = [tuple(row.values()) for row in rows]
                    except:
                        self.rows = rows
            elif isinstance(first_row, (list, tuple)):
                # Уже готовый список кортежей
                try:
                    self.rows = [tuple(row) if not isinstance(row, tuple) else row for row in rows]
                    logger.info(f"📦 List/tuple format parsed: {len(self.rows)} rows")
                    logger.debug(f"Rows: {self.rows}")
                except Exception as e:
                    logger.error(f"❌ Error parsing list format: {e}")
                    self.rows = []
            else:
                logger.warning(f"⚠️ Unknown format: {type(first_row)}")
                self.rows = rows

class TursoClient:
    """Синхронный клиент для Turso БД через REST API"""
    
    def __init__(self, url: str, auth_token: str):
        if url.startswith("libsql://"):
            url = url.replace("libsql://", "https://", 1)
        
        self.url = url.rstrip("/")
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
    
    def execute(self, query: str, args: list = None):
        """Виконати SQL запит. args — список значень для підстановки замість ?"""
        try:
            stmt = {"sql": query}
            if args:
                # Turso вимагає value завжди як рядок
                turso_args = []
                for a in args:
                    if a is None:
                        turso_args.append({"type": "null", "value": None})
                    elif isinstance(a, bool):
                        turso_args.append({"type": "integer", "value": str(int(a))})
                    elif isinstance(a, int):
                        turso_args.append({"type": "integer", "value": str(a)})
                    elif isinstance(a, float):
                        turso_args.append({"type": "float", "value": str(a)})
                    else:
                        turso_args.append({"type": "text", "value": str(a)})
                stmt["args"] = turso_args

            payload = {
                "requests": [
                    {"type": "execute", "stmt": stmt},
                    {"type": "close"}
                ]
            }

            url = f"{self.url}/v2/pipeline"
            logger.info(f"📡 SQL: {query[:120]}")

            response = requests.post(
                url,
                json=payload,
                headers=self.headers,
                timeout=10
            )

            logger.debug(f"HTTP status: {response.status_code}")

            if response.status_code != 200:
                error_msg = f"DB Error ({response.status_code}): {response.text[:300]}"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)

            result = response.json()
            # Логуємо повну відповідь для налагодження
            logger.info(f"📥 RAW response: {json.dumps(result, default=str)[:800]}")

            # Turso /v2/pipeline повертає: {"results": [{"type": "ok", "response": {...}}, ...]}
            results = result.get("results", [])
            if not results:
                logger.warning("⚠️ Empty results array")
                return QueryResult([])

            first = results[0]

            # Перевірка на помилку
            if first.get("type") == "error":
                err = first.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                logger.error(f"❌ DB Error: {msg}")
                raise Exception(f"DB Error: {msg}")

            # Успішна відповідь
            response_obj = first.get("response", {})
            result_inner = response_obj.get("result", {}) if isinstance(response_obj, dict) else {}

            # Спочатку пробуємо result_inner.rows (стандарт v2)
            rows = None
            if isinstance(result_inner, dict):
                rows = result_inner.get("rows")
                logger.debug(f"result_inner keys: {list(result_inner.keys())}, rows count: {len(rows) if rows is not None else 'None'}")

            # Запасний варіант: rows прямо в response_obj
            if rows is None and isinstance(response_obj, dict):
                rows = response_obj.get("rows")

            if rows is not None:
                logger.info(f"✅ SELECT: {len(rows)} rows")
                return QueryResult(rows)

            # INSERT/UPDATE/DELETE — rows немає, це нормально
            logger.debug("No rows (INSERT/UPDATE/DELETE or empty SELECT)")
            return QueryResult([])

        except Exception as e:
            logger.error(f"❌ Query error: {e}", exc_info=True)
            raise

client = None
db_initialized = False

def init_client():
    """Ініціалізація клієнта Turso"""
    global client
    try:
        logger.info(f"🔗 Connecting to: {TURSO_URL}")
        
        if not TURSO_URL or not TURSO_TOKEN:
            logger.error("❌ TURSO_URL or TURSO_TOKEN not set")
            return False
        
        client = TursoClient(url=TURSO_URL, auth_token=TURSO_TOKEN)
        
        try:
            result = client.execute("SELECT 1 as test")
            logger.info("✅ Connection successful")
            return True
        except Exception as test_error:
            logger.error(f"❌ Test failed: {test_error}")
            client = None
            return False
            
    except Exception as e:
        logger.error(f"❌ Client error: {e}")
        client = None
        return False

def get_db_client(retry_count=0):
    """Отримати клієнта БД з повторними спробами"""
    global client
    
    try:
        if client is None:
            if retry_count < MAX_DB_RETRIES:
                logger.warning(f"⚠️ Retry {retry_count + 1}...")
                time.sleep(DB_RETRY_DELAY)
                if init_client():
                    return client
                else:
                    return get_db_client(retry_count + 1)
            else:
                logger.error(f"❌ Failed after {MAX_DB_RETRIES} retries")
                return None
        
        try:
            client.execute("SELECT 1 as test")
            return client
        except Exception as e:
            logger.warning(f"⚠️ Connection lost: {e}")
            client = None
            return get_db_client(retry_count)
            
    except Exception as e:
        logger.error(f"❌ DB client error: {e}")
        return None

def init_db():
    """Ініціалізація таблиць БД"""
    global db_initialized
    try:
        db = get_db_client()
        if db is None:
            logger.error("❌ Failed to connect")
            return False
        
        logger.info("📋 Checking trainers table...")
        
        try:
            result = db.execute("SELECT COUNT(*) as cnt FROM trainers")
            count = result.rows[0][0] if result.rows else 0
            logger.info(f"✅ Table exists, records: {count}")
        except:
            logger.warning(f"⚠️ Table not found, creating...")
            try:
                db.execute("""
                    CREATE TABLE trainers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.info("✅ Table created")
            except Exception as e:
                logger.error(f"❌ Error creating table: {e}")
                return False
        
        db_initialized = True
        logger.info("✅ DB ready")
        return True
        
    except Exception as e:
        logger.error(f"❌ DB error: {e}")
        db_initialized = False
        return False

def escape_sql(text: str) -> str:
    """Экранировать одиночные кавычки"""
    if text is None:
        return ""
    return str(text).replace("'", "''")

# =========================
# 🏁 СТАРТ БОТА
# =========================

@bot.message_handler(commands=['start'])
def start(message):
    """Головне меню"""
    try:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        if message.from_user.id == ADMIN_ID:
            markup.add("Edit")
        
        bot.send_message(
            message.chat.id,
            "♟️ Ласкаво просимо до шахматної школи!\nВиберіть дію:",
            reply_markup=markup
        )
        user_states[message.chat.id] = "main_menu"
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# =========================
# 👨‍💼 АДМІН-ПАНЕЛЬ
# =========================

@bot.message_handler(func=lambda message: message.text == "Edit")
def admin_panel(message):
    """Адмін-панель"""
    try:
        if message.from_user.id != ADMIN_ID:
            bot.send_message(message.chat.id, f"❌ Access denied (your ID: {message.from_user.id})")
            return
        
        user_states[message.chat.id] = "admin_panel"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("➕ Додати тренера", "➖ Видалити тренера")
        markup.add("📋 Список тренерів")
        markup.add("⬅️ Назад")
        
        bot.send_message(message.chat.id, "👨‍💼 Admin panel:", reply_markup=markup)
        logger.info(f"✅ Admin {message.from_user.id} logged in")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: message.text == "⬅️ Назад")
def back_to_menu(message):
    """Повернення в меню"""
    try:
        user_states[message.chat.id] = "main_menu"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        if message.from_user.id == ADMIN_ID:
            markup.add("Edit")
        bot.send_message(message.chat.id, "🔙 Back to menu", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# ===== ДОДАВАННЯ ТРЕНЕРА =====

@bot.message_handler(func=lambda message: message.text == "➕ Додати тренера")
def add_trainer_start(message):
    """Додавання тренера"""
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        user_states[message.chat.id] = "waiting_trainer_username"
        bot.send_message(message.chat.id, "Enter @username:\n(Example: @chess_coach_ivan)")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_username")
def get_trainer_username(message):
    """Username тренера"""
    try:
        username = message.text.strip()
        
        if not username.startswith("@"):
            bot.send_message(message.chat.id, "❌ Must start with @\nTry again:")
            return
        
        clean_username = username[1:]
        trainer_data[message.chat.id] = {"username": clean_username, "display_username": username}
        user_states[message.chat.id] = "waiting_trainer_name"
        bot.send_message(message.chat.id, "Enter trainer name:")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_name")
def get_trainer_name(message):
    """Ім'я тренера"""
    try:
        trainer_data[message.chat.id]["name"] = message.text
        user_states[message.chat.id] = "waiting_trainer_description"
        bot.send_message(message.chat.id, "Enter description:")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_description")
def get_trainer_description(message):
    """Опис та збереження"""
    try:
        trainer_data[message.chat.id]["description"] = message.text
        data = trainer_data[message.chat.id]
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ DB error")
            user_states.pop(message.chat.id, None)
            trainer_data.pop(message.chat.id, None)
            return
        
        try:
            query = "INSERT INTO trainers (username, name, description) VALUES (?, ?, ?)"
            args = [data["username"], data["name"], data["description"]]
            
            logger.info(f"📤 Adding trainer: {data['name']}")
            db.execute(query, args)
            
            logger.info(f"✅ Trainer added: {data['name']} ({data['display_username']})")
            bot.send_message(message.chat.id, f"✅ Trainer {data['name']} added!")
            
        except Exception as db_error:
            error_str = str(db_error).lower()
            logger.error(f"❌ DB error: {db_error}")
            
            if "unique" in error_str or "constraint" in error_str:
                bot.send_message(message.chat.id, f"❌ {data['display_username']} exists")
            else:
                bot.send_message(message.chat.id, f"❌ Error: {db_error}")
        
        user_states.pop(message.chat.id, None)
        trainer_data.pop(message.chat.id, None)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# ===== ВИДАЛЕННЯ ТРЕНЕРА =====

@bot.message_handler(func=lambda message: message.text == "➖ Видалити тренера")
def delete_trainer_start(message):
    """Список для видалення"""
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ DB error")
            return
        
        try:
            logger.info("🔍 Fetching trainers...")
            result = db.execute("SELECT id, name FROM trainers ORDER BY name")
            trainers = result.rows if result.rows else []
            
            logger.info(f"📋 Found {len(trainers)} trainers: {trainers}")
            
            if not trainers:
                logger.warning("⚠️ No trainers!")
                bot.send_message(message.chat.id, "📭 No trainers")
                return
            
            markup = types.InlineKeyboardMarkup()
            
            for trainer in trainers:
                try:
                    logger.debug(f"Adding button for: {trainer}")
                    trainer_id = int(trainer[0])
                    name = str(trainer[1])
                    btn = types.InlineKeyboardButton(text=f"❌ {name}", callback_data=f"delete_trainer_{trainer_id}")
                    markup.add(btn)
                except Exception as e:
                    logger.error(f"❌ Error processing trainer {trainer}: {e}")
            
            bot.send_message(message.chat.id, f"Select trainer ({len(trainers)}):", reply_markup=markup)
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            bot.send_message(message.chat.id, f"❌ Error: {e}")
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_trainer_"))
def delete_trainer_confirm(call):
    """Видалення"""
    try:
        if call.from_user.id != ADMIN_ID:
            return
        
        trainer_id = int(call.data.split("_")[2])
        
        db = get_db_client()
        if db is None:
            bot.answer_callback_query(call.id, "❌ DB error", show_alert=True)
            return
        
        try:
            result = db.execute(f"SELECT name FROM trainers WHERE id = {trainer_id}")
            trainer = result.rows[0] if result.rows else None
            
            if not trainer:
                bot.answer_callback_query(call.id, "❌ Not found", show_alert=True)
                return
            
            trainer_name = str(trainer[0])
            
            db.execute(f"DELETE FROM trainers WHERE id = {trainer_id}")
            
            logger.info(f"✅ Deleted: {trainer_name}")
            bot.answer_callback_query(call.id, "✅ Done!", show_alert=False)
            bot.edit_message_text(f"✅ Trainer '{trainer_name}' deleted", call.message.chat.id, call.message.message_id)
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# ===== СПИСОК ТРЕНЕРІВ =====

@bot.message_handler(func=lambda message: message.text == "📋 Список тренерів")
def list_trainers(message):
    """Список"""
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ DB error")
            return
        
        try:
            logger.info("🔍 Fetching all trainers...")
            result = db.execute("SELECT id, name, username, description FROM trainers ORDER BY name")
            trainers = result.rows if result.rows else []
            
            logger.info(f"📋 Found {len(trainers)} trainers: {trainers}")
            
            if not trainers:
                logger.warning("⚠️ No trainers!")
                bot.send_message(message.chat.id, "📭 No trainers")
                return
            
            text = f"📋 **Trainers** ({len(trainers)}):\n\n"
            for idx, trainer in enumerate(trainers, 1):
                try:
                    logger.debug(f"Row {idx}: {trainer}, type: {type(trainer)}")
                    trainer_id = trainer[0]
                    name = str(trainer[1])
                    username = str(trainer[2])
                    desc = str(trainer[3]) if trainer[3] else "No description"
                    text += f"{idx}. **{name}** (@{username})\n_{desc}_\n\n"
                except Exception as e:
                    logger.error(f"❌ Error processing row {idx}: {e}, trainer={trainer}")
            
            bot.send_message(message.chat.id, text, parse_mode="Markdown")
            logger.info(f"✅ Sent {len(trainers)} trainers")
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            bot.send_message(message.chat.id, f"❌ Error: {e}")
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# =========================
# 👤 ВИБІР ТРЕНЕРА
# =========================

@bot.message_handler(func=lambda message: message.text == "Вибрати тренера")
def choose_trainer_start(message):
    """Вибір тренера"""
    try:
        user_states[message.chat.id] = "waiting_phone"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn = types.KeyboardButton("📱 Send number", request_contact=True)
        markup.add(btn)
        markup.add("⬅️ Back")
        
        bot.send_message(message.chat.id, "Share your number:", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(content_types=['contact'])
def get_phone(message):
    """Номер"""
    try:
        if user_states.get(message.chat.id) != "waiting_phone":
            return
        
        user_form[message.chat.id] = {"phone": message.contact.phone_number}
        user_states[message.chat.id] = "waiting_user_name"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("⬅️ Cancel")
        
        bot.send_message(message.chat.id, "Enter your name:", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_user_name")
def get_user_name(message):
    """Ім'я користувача"""
    try:
        if message.text == "⬅️ Cancel":
            cancel_selection(message)
            return
        
        user_form[message.chat.id]["name"] = message.text
        user_states[message.chat.id] = "waiting_level"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Beginner", "Amateur", "Advanced", "Expert")
        markup.add("⬅️ Cancel")
        
        bot.send_message(message.chat.id, "Your level:", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_level")
def get_level(message):
    """Рівень гри"""
    try:
        if message.text == "⬅️ Cancel":
            cancel_selection(message)
            return
        
        user_form[message.chat.id]["level"] = message.text
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ DB error")
            cancel_selection(message)
            return
        
        try:
            logger.info("🔍 Fetching trainers...")
            result = db.execute("SELECT id, name, description FROM trainers ORDER BY name")
            trainers = result.rows if result.rows else []
            
            logger.info(f"📋 Found {len(trainers)} trainers: {trainers}")
            
            if not trainers:
                logger.warning("⚠️ No trainers!")
                bot.send_message(message.chat.id, "❌ No trainers available")
                cancel_selection(message)
                return
            
            markup = types.InlineKeyboardMarkup()
            
            for trainer in trainers:
                try:
                    logger.debug(f"Adding button for: {trainer}")
                    trainer_id = int(trainer[0])
                    name = str(trainer[1])
                    btn = types.InlineKeyboardButton(text=f"👨‍🏫 {name}", callback_data=f"choose_trainer_{trainer_id}")
                    markup.add(btn)
                except Exception as e:
                    logger.error(f"❌ Error: {e}, trainer={trainer}")
            
            bot.send_message(message.chat.id, f"Select trainer ({len(trainers)}):", reply_markup=markup)
            user_states[message.chat.id] = "trainer_selected"
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            bot.send_message(message.chat.id, f"❌ Error: {e}")
            cancel_selection(message)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        cancel_selection(message)

@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_trainer_"))
def send_request_to_trainer(call):
    """Заявка тренеру"""
    try:
        trainer_id = int(call.data.split("_")[2])
        
        db = get_db_client()
        if db is None:
            bot.answer_callback_query(call.id, "❌ DB error", show_alert=True)
            return
        
        try:
            result = db.execute(f"SELECT username, name FROM trainers WHERE id = {trainer_id}")
            trainer = result.rows[0] if result.rows else None
            
            if not trainer:
                bot.answer_callback_query(call.id, "❌ Not found", show_alert=True)
                return
            
            username = str(trainer[0])
            trainer_name = str(trainer[1])
            username_with_at = f"@{username}"
            data = user_form.get(call.message.chat.id)
            
            if not data:
                bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
                return
            
            notification_text = f"""🎯 **New request!**

👤 Name: {data['name']}
📱 Phone: {data['phone']}
♟️ Level: {data['level']}"""
            
            try:
                bot.send_message(username_with_at, notification_text, parse_mode="Markdown")
                logger.info(f"✅ Request sent to @{username}")
                bot.answer_callback_query(call.id, "✅ Sent!", show_alert=False)
            except Exception as send_error:
                logger.warning(f"⚠️ Error: {send_error}")
                bot.send_message(call.message.chat.id, "⚠️ Error sending")
            
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
            
            bot.edit_message_text(f"✅ Request sent to {trainer_name}!", call.message.chat.id, call.message.message_id)
            bot.send_message(call.message.chat.id, "What next?", reply_markup=markup)
            
            user_states.pop(call.message.chat.id, None)
            user_form.pop(call.message.chat.id, None)
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")

def cancel_selection(message):
    """Скасування"""
    try:
        user_states.pop(message.chat.id, None)
        user_form.pop(message.chat.id, None)
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        if message.from_user.id == ADMIN_ID:
            markup.add("Edit")
        
        bot.send_message(message.chat.id, "Cancelled. Menu:", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# =========================
# 💬 ЧАТ З АДМІНІСТРАТОРОМ
# =========================

@bot.message_handler(func=lambda message: message.text == "Зв'язатися з адміністратором")
def contact_admin_start(message):
    """Запит на чат"""
    try:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🛑 End chat")
        
        bot.send_message(message.chat.id, "⏳ Waiting for admin...", reply_markup=markup)
        user_states[message.chat.id] = "waiting_admin_response"
        
        admin_markup = types.InlineKeyboardMarkup()
        admin_markup.add(types.InlineKeyboardButton("✅ Accept", callback_data=f"accept_chat_{message.chat.id}"))
        admin_markup.add(types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_chat_{message.chat.id}"))
        
        user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.chat.id}"
        
        bot.send_message(ADMIN_ID, f"📞 Request from: {user_info}\nName: {message.from_user.first_name}", reply_markup=admin_markup)
        logger.info(f"📞 Request from: {user_info}")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_chat_"))
def accept_chat(call):
    """Прийняти чат"""
    try:
        user_id = int(call.data.split("_")[2])
        
        if user_id in admin_chats:
            bot.answer_callback_query(call.id, "⚠️ Already active", show_alert=True)
            return
        
        admin_chats[user_id] = call.from_user.id
        user_states[user_id] = "in_admin_chat"
        
        bot.edit_message_text("✅ Accepted", call.message.chat.id, call.message.message_id)
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🛑 End chat")
        
        bot.send_message(user_id, "✅ Admin accepted!", reply_markup=markup)
        logger.info(f"✅ Chat: {user_id}")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_chat_"))
def reject_chat(call):
    """Відхилити чат"""
    try:
        user_id = int(call.data.split("_")[2])
        bot.edit_message_text("❌ Rejected", call.message.chat.id, call.message.message_id)
        user_states[user_id] = "main_menu"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        
        bot.send_message(user_id, "❌ Rejected", reply_markup=markup)
        logger.info(f"❌ Chat: {user_id}")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: message.text == "🛑 End chat")
def end_chat(message):
    """Завершити чат"""
    try:
        if message.chat.id in admin_chats:
            admin_id = admin_chats[message.chat.id]
            bot.send_message(message.chat.id, "👋 Thank you!")
            try:
                bot.send_message(admin_id, f"👤 User ended chat")
            except:
                pass
            admin_chats.pop(message.chat.id, None)
        elif message.from_user.id == ADMIN_ID:
            user_id = None
            for uid, aid in admin_chats.items():
                if aid == message.from_user.id:
                    user_id = uid
                    break
            
            if user_id:
                try:
                    bot.send_message(user_id, "👋 Admin ended chat")
                except:
                    pass
                admin_chats.pop(user_id, None)
        
        user_states[message.chat.id] = "main_menu"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        if message.from_user.id == ADMIN_ID:
            markup.add("Edit")
        bot.send_message(message.chat.id, "Menu:", reply_markup=markup)
        logger.info(f"👋 Chat ended: {message.chat.id}")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: message.chat.id in admin_chats and user_states.get(message.chat.id) == "in_admin_chat")
def relay_user_message(message):
    """Від користувача до адміна"""
    try:
        if message.text == "🛑 End chat":
            end_chat(message)
            return
        
        admin_id = admin_chats[message.chat.id]
        try:
            bot.send_message(admin_id, f"💬 {message.text}")
        except:
            pass
    except Exception as e:
        logger.error(f"❌ Error: {e}")

@bot.message_handler(func=lambda message: message.from_user.id == ADMIN_ID)
def relay_admin_message(message):
    """Від адміна до користувача"""
    try:
        if message.text == "🛑 End chat":
            end_chat(message)
            return
        
        user_id = None
        for uid, aid in admin_chats.items():
            if aid == message.from_user.id:
                user_id = uid
                break
        
        if not user_id:
            bot.send_message(message.chat.id, "❌ No chat")
            return
        
        try:
            bot.send_message(user_id, f"💬 Admin: {message.text}")
        except:
            bot.send_message(message.chat.id, "❌ Error")
    except Exception as e:
        logger.error(f"❌ Error: {e}")

# =========================
# 🌐 FLASK ENDPOINTS
# =========================

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook"""
    try:
        json_data = request.get_json()
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"❌ Webhook: {e}")
    return '', 200

@app.route('/debug', methods=['GET'])
def debug_db():
    """Діагностика БД — показує сирий відповідь Turso і список тренерів"""
    try:
        import json as _json

        # 1. Сирий запит до Turso
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": "SELECT id, name, username, description FROM trainers"}},
                {"type": "close"}
            ]
        }
        headers = {
            "Authorization": f"Bearer {TURSO_TOKEN}",
            "Content-Type": "application/json"
        }
        raw = requests.post(f"{TURSO_URL}/v2/pipeline", json=payload, headers=headers, timeout=10)
        raw_json = raw.json()

        # 2. Через наш клієнт
        db = get_db_client()
        parsed_rows = []
        if db:
            try:
                result = db.execute("SELECT id, name, username, description FROM trainers")
                parsed_rows = [list(r) for r in result.rows]
            except Exception as e:
                parsed_rows = [f"ERROR: {e}"]

        return _json.dumps({
            "http_status": raw.status_code,
            "raw_turso_response": raw_json,
            "parsed_rows": parsed_rows
        }, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return _json.dumps({"error": str(e)}, ensure_ascii=False), 500


def health():
    """Health"""
    try:
        db = get_db_client()
        return 'OK' if db else 'ERROR', 200 if db else 500
    except:
        return 'ERROR', 500

# =========================
# 🚀 ЗАПУСК
# =========================

if __name__ == "__main__":
    logger.info("🚀 Starting...")
    
    if not init_db():
        logger.error("❌ DB error")
    
    try:
        bot.remove_webhook()
        logger.info("✅ Webhook removed")
    except:
        pass
    
    webhook_url = f"{WEBHOOK_URL}/webhook"
    try:
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Webhook: {e}")
    
    port = int(os.getenv("PORT", 5000))
    logger.info(f"🌐 Running on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
