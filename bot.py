import telebot
from telebot import types
import os
import logging
import json
from flask import Flask, request
import time
from functools import wraps
import requests

# =========================
# 📝 ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================
# 🔐 НАЛАШТУВАННЯ
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ТВІЙ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "887078537"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://78655.onrender.com")

TURSO_URL = os.getenv("TURSO_URL", "libsql://1qaz2wsx-yhbvgt65.aws-eu-west-1.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJleHAiOjE4MDc4NjA1NDEsImlhdCI6MTc3NjMyNDU0MSwiaWQiOiIwMTlkOTUyZC03YjAxLTc3N2QtYjE4NS03MDEzY2JjOWYwMDkiLCJyaWQiOiI3NmJlZDlhMy01Zjk1LTQ0OGYtYThkYi1kZTY2OTNmNjcwZTAifQ.fN9MZ5inviHOnUNqhrW20hbt1oUmHS6E2auA_grZ6pcv02NvEKEmrI5Ms_oSnwbBM1nTsR-TmE7SSIrB4utKDw")

# Максимальное количество попыток переподключения
MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 2  # секунды

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

class TursoClient:
    """Синхронный клиент для Turso БД через REST API"""
    
    def __init__(self, url: str, auth_token: str):
        self.url = url
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
    
    def execute(self, query: str, params: list = None):
        """Выполнить SQL запрос"""
        try:
            payload = {
                "statements": [
                    {
                        "sql": query,
                        "args": params or []
                    }
                ]
            }
            
            response = requests.post(
                f"{self.url}/v2/pipeline",
                json=payload,
                headers=self.headers,
                timeout=10
            )
            
            if response.status_code != 200:
                raise Exception(f"DB Error: {response.text}")
            
            result = response.json()
            
            # Обработка результатов
            if result.get("results"):
                result_data = result["results"][0]
                if result_data.get("rows"):
                    return QueryResult(result_data["rows"])
                return QueryResult([])
            
            return QueryResult([])
        except Exception as e:
            logger.error(f"❌ Ошибка выполнения запроса: {e}")
            raise

class QueryResult:
    """Результат запроса к БД"""
    def __init__(self, rows):
        self.rows = rows

client = None
db_initialized = False

def init_client():
    """Ініціалізація клієнта Turso з обробкою помилок"""
    global client
    try:
        logger.info(f"🔗 Спроба підключення до Turso: {TURSO_URL}")
        
        # Перевірка наявності необхідних параметрів
        if not TURSO_URL or not TURSO_TOKEN:
            logger.error("❌ TURSO_URL або TURSO_TOKEN не встановлені!")
            return False
        
        client = TursoClient(url=TURSO_URL, auth_token=TURSO_TOKEN)
        
        # Тестове підключення
        try:
            result = client.execute("SELECT 1")
            logger.info("✅ Підключення до Turso успішне")
            return True
        except Exception as test_error:
            logger.error(f"❌ Тестове підключення не вдалось: {test_error}")
            client = None
            return False
            
    except Exception as e:
        logger.error(f"❌ Помилка ініціалізації клієнта: {e}")
        client = None
        return False

def get_db_client(retry_count=0):
    """Отримати клієнта БД з повторними спробами"""
    global client
    
    try:
        if client is None:
            if retry_count < MAX_DB_RETRIES:
                logger.warning(f"⚠️ Повна {retry_count + 1} спроба переподключення...")
                time.sleep(DB_RETRY_DELAY)
                if init_client():
                    return client
                else:
                    return get_db_client(retry_count + 1)
            else:
                logger.error(f"❌ Не вдалось підключитися після {MAX_DB_RETRIES} спроб")
                return None
        
        # Тест живого з'єднання
        try:
            client.execute("SELECT 1")
            return client
        except Exception as e:
            logger.warning(f"⚠️ З'єднання втрачено: {e}. Переп'єднання...")
            client = None
            return get_db_client(retry_count)
            
    except Exception as e:
        logger.error(f"❌ Помилка отримання DB клієнта: {e}")
        return None

def db_operation(func):
    """Декоратор для надійних операцій з БД"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            db = get_db_client()
            if db is None:
                logger.error(f"❌ Помилка підключення до БД у {func.__name__}")
                return None
            return func(db, *args, **kwargs)
        except Exception as e:
            logger.error(f"❌ Помилка виконання {func.__name__}: {e}")
            return None
    return wrapper

def init_db():
    """Ініціалізація таблиць БД"""
    global db_initialized
    try:
        db = get_db_client()
        if db is None:
            logger.error("❌ Не вдалось підключитися до БД на етапі ініціалізації")
            return False
        
        logger.info("📋 Створення таблиці trainers...")
        
        # Спочатку перевіримо чи таблиця існує
        try:
            db.execute("SELECT COUNT(*) FROM trainers")
            logger.info("✅ Таблиця trainers вже існує")
        except:
            # Таблиця не існує, створюємо
            db.execute("""
                CREATE TABLE trainers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info("✅ Таблиця trainers успішно створена")
        
        db_initialized = True
        logger.info("✅ База даних ініціалізована")
        return True
        
    except Exception as e:
        logger.error(f"❌ Помилка ініціалізації БД: {e}")
        db_initialized = False
        return False

# =========================
# 🏁 СТАРТ БОТА
# =========================

@bot.message_handler(commands=['start'])
def start(message):
    """Головне меню користувача"""
    try:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        
        bot.send_message(
            message.chat.id,
            "♟️ Ласкаво просимо до шахматної школи!\nВиберіть дію:",
            reply_markup=markup
        )
        user_states[message.chat.id] = "main_menu"
    except Exception as e:
        logger.error(f"❌ Помилка у start: {e}")
        bot.send_message(message.chat.id, "❌ Помилка при запуску. Спробуйте ще раз.")

# =========================
# 👨‍💼 АДМІН-ПАНЕЛЬ
# =========================

@bot.message_handler(func=lambda message: message.text == "Edit")
def admin_panel(message):
    """Доступ до адмін-панелі (тільки для адміністратора)"""
    try:
        if message.from_user.id != ADMIN_ID:
            bot.send_message(message.chat.id, "❌ Немає доступу")
            return
        
        user_states[message.chat.id] = "admin_panel"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("➕ Додати тренера", "➖ Видалити тренера")
        markup.add("📋 Список тренерів")
        
        bot.send_message(
            message.chat.id,
            "👨‍💼 Адміністраторська панель:",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"❌ Помилка у admin_panel: {e}")

# ===== ДОДАВАННЯ ТРЕНЕРА =====

@bot.message_handler(func=lambda message: message.text == "➕ Додати тренера")
def add_trainer_start(message):
    """Початок процесу додавання тренера"""
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        user_states[message.chat.id] = "waiting_trainer_username"
        bot.send_message(
            message.chat.id,
            "Введи @username тренера (з собачкою):\n(Приклад: @chess_coach_ivan)"
        )
    except Exception as e:
        logger.error(f"❌ Помилка у add_trainer_start: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_username")
def get_trainer_username(message):
    """Отримання username тренера"""
    try:
        username = message.text.strip()
        
        if not username.startswith("@"):
            bot.send_message(message.chat.id, "❌ Username має починатися з @\nПопробуй ще раз:")
            return
        
        trainer_data[message.chat.id] = {"username": username}
        user_states[message.chat.id] = "waiting_trainer_name"
        bot.send_message(message.chat.id, "Введи ім'я тренера:")
    except Exception as e:
        logger.error(f"❌ Помилка у get_trainer_username: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_name")
def get_trainer_name(message):
    """Отримання імені тренера"""
    try:
        trainer_data[message.chat.id]["name"] = message.text
        user_states[message.chat.id] = "waiting_trainer_description"
        bot.send_message(message.chat.id, "Введи опис тренера (досвід, кваліфікація тощо):")
    except Exception as e:
        logger.error(f"❌ Помилка у get_trainer_name: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_description")
def get_trainer_description(message):
    """Отримання опису та збереження тренера в БД"""
    try:
        trainer_data[message.chat.id]["description"] = message.text
        data = trainer_data[message.chat.id]
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            return
        
        try:
            db.execute(
                "INSERT INTO trainers (username, name, description) VALUES (?, ?, ?)",
                [data["username"], data["name"], data["description"]]
            )
            
            logger.info(f"✅ Тренер додан: {data['name']} ({data['username']})")
            bot.send_message(
                message.chat.id,
                f"✅ Тренер {data['name']} успішно додан!"
            )
            
        except Exception as db_error:
            error_str = str(db_error).lower()
            if "unique" in error_str or "constraint" in error_str:
                bot.send_message(
                    message.chat.id,
                    f"❌ Тренер з username {data['username']} уже існує"
                )
            else:
                logger.error(f"❌ Помилка БД: {db_error}")
                bot.send_message(message.chat.id, f"❌ Помилка: {db_error}")
        
        user_states.pop(message.chat.id, None)
        trainer_data.pop(message.chat.id, None)
        
    except Exception as e:
        logger.error(f"❌ Помилка у get_trainer_description: {e}")

# ===== ВИДАЛЕННЯ ТРЕНЕРА =====

@bot.message_handler(func=lambda message: message.text == "➖ Видалити тренера")
def delete_trainer_start(message):
    """Показ списку тренерів для видалення"""
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            return
        
        result = db.execute("SELECT id, name FROM trainers ORDER BY name")
        trainers = result.rows if hasattr(result, 'rows') and result.rows else []
        
        if not trainers:
            bot.send_message(message.chat.id, "📭 Список тренерів порожній")
            return
        
        markup = types.InlineKeyboardMarkup()
        
        for trainer in trainers:
            trainer_id = trainer[0]
            name = trainer[1]
            btn = types.InlineKeyboardButton(
                text=f"❌ {name}",
                callback_data=f"delete_trainer_{trainer_id}"
            )
            markup.add(btn)
        
        bot.send_message(message.chat.id, "Вибери тренера для видалення:", reply_markup=markup)
        
    except Exception as e:
        logger.error(f"❌ Помилка у delete_trainer_start: {e}")
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_trainer_"))
def delete_trainer_confirm(call):
    """Видалення тренера"""
    try:
        if call.from_user.id != ADMIN_ID:
            bot.answer_callback_query(call.id, "❌ Немає доступу", show_alert=True)
            return
        
        trainer_id = call.data.split("_")[2]
        
        db = get_db_client()
        if db is None:
            bot.answer_callback_query(call.id, "❌ Помилка підключення", show_alert=True)
            return
        
        result = db.execute("SELECT name FROM trainers WHERE id = ?", [trainer_id])
        trainer = result.rows[0] if (hasattr(result, 'rows') and result.rows) else None
        
        if not trainer:
            bot.answer_callback_query(call.id, "❌ Тренер не знайдений", show_alert=True)
            return
        
        db.execute("DELETE FROM trainers WHERE id = ?", [trainer_id])
        
        logger.info(f"✅ Тренер видалений: {trainer[0]}")
        bot.answer_callback_query(call.id, "✅ Видалено!", show_alert=False)
        bot.edit_message_text(
            f"✅ Тренер '{trainer[0]}' видалений із системи",
            call.message.chat.id,
            call.message.message_id
        )
        
    except Exception as e:
        logger.error(f"❌ Помилка у delete_trainer_confirm: {e}")
        bot.answer_callback_query(call.id, f"❌ Помилка: {e}", show_alert=True)

# ===== СПИСОК ТРЕНЕРІВ =====

@bot.message_handler(func=lambda message: message.text == "📋 Список тренерів")
def list_trainers(message):
    """Показ усіх тренерів"""
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            return
        
        result = db.execute("SELECT id, name, username, description FROM trainers ORDER BY name")
        trainers = result.rows if hasattr(result, 'rows') and result.rows else []
        
        if not trainers:
            bot.send_message(message.chat.id, "📭 Список тренерів порожній")
            return
        
        text = "📋 **Список тренерів:**\n\n"
        for idx, trainer in enumerate(trainers, 1):
            name = trainer[1]
            username = trainer[2]
            desc = trainer[3] or "Немає опису"
            text += f"{idx}. **{name}** ({username})\n"
            text += f"   _{desc}_\n\n"
        
        bot.send_message(message.chat.id, text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"❌ Помилка у list_trainers: {e}")
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

# =========================
# 👤 ВИБІР ТРЕНЕРА (КОРИСТУВАЧ)
# =========================

@bot.message_handler(func=lambda message: message.text == "Вибрат�� тренера")
def choose_trainer_start(message):
    """Початок процесу вибору тренера"""
    try:
        user_states[message.chat.id] = "waiting_phone"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn = types.KeyboardButton("📱 Надіслати номер", request_contact=True)
        markup.add(btn)
        
        bot.send_message(
            message.chat.id,
            "Поділись своїм номером телефону:",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"❌ Помилка у choose_trainer_start: {e}")

@bot.message_handler(content_types=['contact'])
def get_phone(message):
    """Отримання номера телефону"""
    try:
        if user_states.get(message.chat.id) != "waiting_phone":
            return
        
        user_form[message.chat.id] = {}
        user_form[message.chat.id]["phone"] = message.contact.phone_number
        
        user_states[message.chat.id] = "waiting_user_name"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("◀️ Скасувати")
        
        bot.send_message(
            message.chat.id,
            "Спасибі! Тепер введи своє ім'я:",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"❌ Помилка у get_phone: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_user_name")
def get_user_name(message):
    """Отримання імені користувача"""
    try:
        if message.text == "◀️ Скасувати":
            cancel_selection(message)
            return
        
        user_form[message.chat.id]["name"] = message.text
        user_states[message.chat.id] = "waiting_level"
        
        bot.send_message(
            message.chat.id,
            "Опиши свій рівень гри в шахи:\n(Наприклад: Початківець, Середній, Продвинутий)"
        )
    except Exception as e:
        logger.error(f"❌ Помилка у get_user_name: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_level")
def get_level(message):
    """Отримання рівня та показ списку тренерів"""
    try:
        if message.text == "◀️ Скасувати":
            cancel_selection(message)
            return
        
        user_form[message.chat.id]["level"] = message.text
        
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            cancel_selection(message)
            return
        
        result = db.execute("SELECT id, name, description FROM trainers ORDER BY name")
        trainers = result.rows if hasattr(result, 'rows') and result.rows else []
        
        if not trainers:
            bot.send_message(
                message.chat.id,
                "❌ На жаль, зараз немає доступних тренерів. Спробуй пізніше."
            )
            cancel_selection(message)
            return
        
        markup = types.InlineKeyboardMarkup()
        
        for trainer in trainers:
            trainer_id = trainer[0]
            name = trainer[1]
            btn = types.InlineKeyboardButton(
                text=f"👨‍🏫 {name}",
                callback_data=f"choose_trainer_{trainer_id}"
            )
            markup.add(btn)
        
        bot.send_message(
            message.chat.id,
            "Вибери свого тренера:",
            reply_markup=markup
        )
        user_states[message.chat.id] = "trainer_selected"
        
    except Exception as e:
        logger.error(f"❌ Помилка у get_level: {e}")
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        cancel_selection(message)

@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_trainer_"))
def send_request_to_trainer(call):
    """Надіслання заявки вибраному тренеру"""
    try:
        trainer_id = call.data.split("_")[2]
        
        db = get_db_client()
        if db is None:
            bot.answer_callback_query(call.id, "❌ Помилка підключення", show_alert=True)
            return
        
        result = db.execute(
            "SELECT username, name FROM trainers WHERE id = ?",
            [trainer_id]
        )
        trainer = result.rows[0] if (hasattr(result, 'rows') and result.rows) else None
        
        if not trainer:
            bot.answer_callback_query(call.id, "❌ Тренер не знайдений", show_alert=True)
            return
        
        username, trainer_name = trainer
        data = user_form.get(call.message.chat.id)
        
        if not data:
            bot.answer_callback_query(call.id, "❌ Помилка даних", show_alert=True)
            return
        
        # Надіслання повідомлення тренеру
        notification_text = f"""
🎯 **Нова заявка на заняття!**

👤 **Ім'я:** {data['name']}
📱 **Телефон:** {data['phone']}
♟️ **Рівень:** {data['level']}

Тренер, зв'яжись з учнем!
        """
        
        try:
            bot.send_message(username, notification_text, parse_mode="Markdown")
            logger.info(f"✅ Заявка надіслана тренеру {trainer_name}")
            bot.answer_callback_query(call.id, "✅ Заявка надіслана тренеру!", show_alert=False)
        except Exception as send_error:
            logger.warning(f"⚠️ Не вдалось надіслати заявку: {send_error}")
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Не вдалось надіслати заявку тренеру. Перевір контакти адміністратора."
            )
        
        # Підтвердження користувачу
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати іншого тренера", "Зв'язатися з адміністратором")
        
        bot.edit_message_text(
            f"✅ Твоя заявка надіслана тренеру {trainer_name}!\nВін зв'яжеться з тобою найближчим часом.",
            call.message.chat.id,
            call.message.message_id
        )
        
        bot.send_message(
            call.message.chat.id,
            "Що дальше?",
            reply_markup=markup
        )
        
        # Очистка даних
        user_states.pop(call.message.chat.id, None)
        user_form.pop(call.message.chat.id, None)
        
    except Exception as e:
        logger.error(f"❌ Помилка у send_request_to_trainer: {e}")
        bot.answer_callback_query(call.id, f"❌ Помилка: {e}", show_alert=True)

def cancel_selection(message):
    """Скасування вибору тренера"""
    try:
        user_states.pop(message.chat.id, None)
        user_form.pop(message.chat.id, None)
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адмініст��атором")
        
        bot.send_message(message.chat.id, "Скасовано. Головне меню:", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Помилка у cancel_selection: {e}")

# =========================
# 💬 ЧАТ З АДМІНІСТРАТОРОМ
# =========================

@bot.message_handler(func=lambda message: message.text == "Зв'язатися з адміністратором")
def contact_admin_start(message):
    """Ініціація чату з адміністратором"""
    try:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🛑 Завершити чат")
        
        bot.send_message(
            message.chat.id,
            "⏳ Очікуй відповіді адміністратора...\nАдміністратор скоро з вами зв'яжеться!",
            reply_markup=markup
        )
        
        user_states[message.chat.id] = "waiting_admin_response"
        
        # Надіслання повідомлення адміністратору
        admin_markup = types.InlineKeyboardMarkup()
        admin_markup.add(
            types.InlineKeyboardButton(
                "✅ Прийняти чат",
                callback_data=f"accept_chat_{message.chat.id}"
            )
        )
        admin_markup.add(
            types.InlineKeyboardButton(
                "❌ Відхилити",
                callback_data=f"reject_chat_{message.chat.id}"
            )
        )
        
        user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.chat.id}"
        
        bot.send_message(
            ADMIN_ID,
            f"📞 **Запит на чат від користувача**\n\nКористувач: {user_info}\nІм'я: {message.from_user.first_name}",
            reply_markup=admin_markup,
            parse_mode="Markdown"
        )
        logger.info(f"📞 Запит на чат від {user_info}")
    except Exception as e:
        logger.error(f"❌ Помилка у contact_admin_start: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_chat_"))
def accept_chat(call):
    """Адміністратор приймає чат"""
    try:
        user_id = int(call.data.split("_")[2])
        
        if user_id in admin_chats:
            bot.answer_callback_query(call.id, "⚠️ Чат уже активний з іншим адміном", show_alert=True)
            return
        
        admin_chats[user_id] = call.from_user.id
        user_states[user_id] = "in_admin_chat"
        
        bot.edit_message_text(
            "✅ Чат прийнято! Починаємо спілкування.",
            call.message.chat.id,
            call.message.message_id
        )
        
        try:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("🛑 Завершити чат")
            
            bot.send_message(
                user_id,
                "✅ Адміністратор прийняв вашу заявку!\n💬 Тепер ви можете спілкуватися з ним напряму.",
                reply_markup=markup
            )
            logger.info(f"✅ Чат прийнято для користувача {user_id}")
        except:
            pass
    except Exception as e:
        logger.error(f"❌ Помилка у accept_chat: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_chat_"))
def reject_chat(call):
    """Адміністратор відхиляє чат"""
    try:
        user_id = int(call.data.split("_")[2])
        
        bot.edit_message_text(
            "❌ Чат відхилений.",
            call.message.chat.id,
            call.message.message_id
        )
        
        user_states[user_id] = "main_menu"
        
        try:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
            
            bot.send_message(
                user_id,
                "❌ Адміністратор відхилив вашу заявку. Спробуй пізніше.",
                reply_markup=markup
            )
            logger.info(f"❌ Чат відхилено для користувача {user_id}")
        except:
            pass
    except Exception as e:
        logger.error(f"❌ Помилка у reject_chat: {e}")

@bot.message_handler(func=lambda message: message.text == "🛑 Завершити чат")
def end_chat(message):
    """Завершення чату (користувач або адміністратор)"""
    try:
        if message.chat.id in admin_chats:
            admin_id = admin_chats[message.chat.id]
            
            bot.send_message(
                message.chat.id,
                "👋 Чат завершено. Спасибі за звернення!"
            )
            
            try:
                bot.send_message(
                    admin_id,
                    f"👤 Користувач завершив чат (ID: {message.chat.id})"
                )
            except:
                pass
            
            admin_chats.pop(message.chat.id, None)
        elif message.from_user.id == ADMIN_ID:
            # Адміністратор завершує чат
            user_id = None
            for uid, aid in admin_chats.items():
                if aid == message.from_user.id:
                    user_id = uid
                    break
            
            if user_id:
                try:
                    bot.send_message(user_id, "👋 Адміністратор завершив чат.")
                except:
                    pass
                admin_chats.pop(user_id, None)
            else:
                bot.send_message(message.chat.id, "❌ Немає активного чату")
                return
        
        user_states[message.chat.id] = "main_menu"
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
        bot.send_message(message.chat.id, "Головне меню:", reply_markup=markup)
        logger.info(f"👋 Чат завершено для користувача {message.chat.id}")
    except Exception as e:
        logger.error(f"❌ Помилка у end_chat: {e}")

@bot.message_handler(func=lambda message: message.chat.id in admin_chats and user_states.get(message.chat.id) == "in_admin_chat")
def relay_user_message(message):
    """Пересилання повідомлення від користувача до адміна"""
    try:
        if message.text == "🛑 Завершити чат":
            end_chat(message)
            return
        
        admin_id = admin_chats[message.chat.id]
        
        try:
            bot.send_message(
                admin_id,
                f"💬 Повідомлення від користувача:\n\n{message.text}"
            )
        except:
            pass
    except Exception as e:
        logger.error(f"❌ Помилка у relay_user_message: {e}")

@bot.message_handler(func=lambda message: message.from_user.id == ADMIN_ID)
def relay_admin_message(message):
    """Пересилання повідомлення від адміна до користувача"""
    try:
        if message.text == "🛑 Завершити чат":
            end_chat(message)
            return
        
        user_id = None
        for uid, aid in admin_chats.items():
            if aid == message.from_user.id:
                user_id = uid
                break
        
        if not user_id:
            bot.send_message(message.chat.id, "❌ Немає активного чату")
            return
        
        try:
            bot.send_message(
                user_id,
                f"💬 Адміністратор:\n\n{message.text}"
            )
        except:
            bot.send_message(message.chat.id, f"❌ Не вдалось надіслати повідомлення користувачу")
    except Exception as e:
        logger.error(f"❌ Помилка у relay_admin_message: {e}")

# =========================
# 🌐 FLASK WEBHOOK ENDPOINTS
# =========================

@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram webhook handler"""
    try:
        json_data = request.get_json()
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"❌ Помилка обробки webhook: {e}")
    return '', 200

@app.route('/health', methods=['GET'])
def health():
    """Health check для Render"""
    try:
        db = get_db_client()
        if db is None:
            return 'DB_ERROR', 500
        return 'OK', 200
    except:
        return 'ERROR', 500

@app.route('/debug', methods=['GET'])
def debug():
    """Отладка стану бота"""
    return {
        'status': 'OK',
        'db_initialized': db_initialized,
        'users_online': len(user_states),
        'admin_chats': len(admin_chats)
    }, 200

# =========================
# 🚀 ЗАПУСК БОТА
# =========================

if __name__ == "__main__":
    logger.info("🚀 Бот запускається...")
    
    # Ініціалізація БД
    if not init_db():
        logger.error("❌ Критична помилка: не вдалось ініціалізувати БД")
    
    # Видалити старий webhook
    try:
        bot.remove_webhook()
        logger.info("✅ Старий webhook видалено")
    except:
        pass
    
    # Встановити новий webhook
    webhook_url = f"{WEBHOOK_URL}/webhook"
    try:
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook встановлено: {webhook_url}")
    except Exception as e:
        logger.error(f"⚠️ Помилка встановлення webhook: {e}")
    
    # Запустити Flask додаток
    port = int(os.getenv("PORT", 5000))
    logger.info(f"🌐 Запуск Flask на порту {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
