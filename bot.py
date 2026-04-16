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
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# =========================
# 🔐 НАЛАШТУВАННЯ
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "12345678"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-webhook-url.onrender.com")

TURSO_URL = os.getenv("TURSO_URL", "https://your-database-url.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "your-turso-auth-token")

MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 2

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# =========================
# 🗄️ КЛАСС ДЛЯ SQL-КЛИЕНТА
# =========================
class TursoClient:
    """Клиент для запросов к базе данных Turso через REST API"""

    def __init__(self, url: str, auth_token: str):
        self.url = url.rstrip("/")
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def execute(self, query: str):
        """Выполнение SQL-запроса (SELECT, INSERT, DELETE)"""
        try:
            payload = {"requests": [{"type": "execute", "stmt": {"sql": query}}]}
            url = f"{self.url}/v2/pipeline"
            logger.debug(f"📡 SQL QUERY: {query}")
            
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()  # Бросает ошибку при 4xx/5xx

            result = response.json()
            logger.debug(f"SQL Response: {json.dumps(result, indent=2)}")
            
            rows = result.get("results", [{}])[0].get("response", {}).get("rows", [])
            return [tuple(row["values"]) for row in rows] if rows else []
        
        except requests.RequestException as e:
            logger.error(f"❌ HTTP Error: {str(e)}")
            raise Exception("SQL query failed") from e


# =========================
# 🗄️ СОЕДИНЕНИЕ С ��Д
# =========================
client = None

def get_db_client(retry_count=0):
    global client
    if retry_count >= MAX_DB_RETRIES:
        logger.error("❌ Max retries to connect to DB reached.")
        return None
    if client is None:
        try:
            client = TursoClient(TURSO_URL, TURSO_TOKEN)
            client.execute("SELECT 1")  # Проверим соединение с базой
            logger.info("✅ Connected to database.")
        except Exception as e:
            logger.error(f"❌ DB Error: {e}. Retrying...")
            time.sleep(DB_RETRY_DELAY)
            return get_db_client(retry_count + 1)
    return client


# =========================
# 📋 КОМАНДЫ БОТА
# =========================
@bot.message_handler(commands=["start"])
def handle_start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📋 List Trainers", "➕ Add Trainer")
    markup.add("➖ Remove Trainer")
    bot.send_message(
        message.chat.id,
        "Welcome to Chess School! 🏫\nChoose an action:",
        reply_markup=markup,
    )


@bot.message_handler(func=lambda msg: msg.text == "📋 List Trainers")
def list_trainers(message):
    db = get_db_client()
    if not db:
        bot.send_message(message.chat.id, "❌ Cannot connect to database.")
        return

    try:
        trainers = db.execute("SELECT id, username, name, description FROM trainers ORDER BY name")
        if not trainers:
            bot.send_message(message.chat.id, "📭 No trainers found.")
            return
        
        response = "📋 List of Trainers:\n\n"
        for idx, (tid, username, name, desc) in enumerate(trainers, 1):
            response += f"{idx}. **{name}** (@{username})\n- {desc or 'No description'}\n\n"
        bot.send_message(message.chat.id, response, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"❌ Error in list_trainers: {e}")
        bot.send_message(message.chat.id, "❌ Unable to fetch trainers.")


@bot.message_handler(func=lambda msg: msg.text == "➕ Add Trainer")
def add_trainer(message):
    bot.send_message(message.chat.id, "Enter the trainer's @username:")
    bot.register_next_step_handler(message, process_trainer_username)


def process_trainer_username(message):
    username = message.text.lstrip("@")
    if not username:
        bot.send_message(message.chat.id, "❌ Invalid username.")
        return
    bot.send_message(message.chat.id, "Enter the trainer's name:")
    bot.register_next_step_handler(message, process_trainer_name, username)


def process_trainer_name(message, username):
    name = message.text.strip()
    bot.send_message(message.chat.id, "Enter a description:")
    bot.register_next_step_handler(message, process_trainer_description, username, name)


def process_trainer_description(message, username, name):
    description = message.text.strip()
    db = get_db_client()
    if not db:
        bot.send_message(message.chat.id, "❌ Cannot connect to database.")
        return
    
    try:
        query = f"""
            INSERT INTO trainers (username, name, description)
            VALUES ('{username}', '{name}', '{description}')
        """
        db.execute(query)
        bot.send_message(message.chat.id, f"✅ Trainer '{name}' added!")
    except Exception as e:
        logger.error(f"❌ Error adding trainer: {e}")
        bot.send_message(message.chat.id, "❌ Could not add trainer.")


@bot.message_handler(func=lambda msg: msg.text == "➖ Remove Trainer")
def remove_trainer(message):
    db = get_db_client()
    if not db:
        bot.send_message(message.chat.id, "❌ Cannot connect to database.")
        return
    
    try:
        trainers = db.execute("SELECT id, name FROM trainers ORDER BY name")
        if not trainers:
            bot.send_message(message.chat.id, "📭 No trainers to remove.")
            return
        
        markup = types.InlineKeyboardMarkup()
        for tid, name in trainers:
            markup.add(types.InlineKeyboardButton(f"Remove {name}", callback_data=f"remove_{tid}"))
        bot.send_message(message.chat.id, "Choose a trainer to remove:", reply_markup=markup)
    except Exception as e:
        logger.error(f"❌ Error in remove_trainer: {e}")
        bot.send_message(message.chat.id, "❌ Could not fetch trainers.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_"))
def confirm_remove_trainer(call):
    db = get_db_client()
    if not db:
        bot.answer_callback_query(call.id, "❌ Cannot connect to database.")
        return

    trainer_id = call.data.split("_")[1]
    try:
        db.execute(f"DELETE FROM trainers WHERE id = {trainer_id}")
        bot.answer_callback_query(call.id, "✅ Trainer removed!")
        bot.send_message(call.message.chat.id, "Trainer successfully removed.")
    except Exception as e:
        logger.error(f"❌ Error removing trainer: {e}")
        bot.answer_callback_query(call.id, "❌ Error removing trainer.")


# =========================
# 🚀 ЗАПУСК БОТА
# =========================
if __name__ == "__main__":
    logger.info("Starting bot...")
    if not get_db_client():
        logger.error("❌ Failed to initialize database connection.")
        exit(1)

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
