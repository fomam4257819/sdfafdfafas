import telebot
from telebot import types
import os
from flask import Flask, request
from libsql_client import create_client

# =========================
# 🔐 НАЛАШТУВАННЯ
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ТВІЙ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-app.onrender.com")

TURSO_URL = os.getenv("TURSO_URL", "libsql://yhbvgt65-yhbvgt65.aws-ap-northeast-1.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "ТВІЙ_ТОКЕН_TURSO")

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

client = None

def init_client():
    """Ініціалізація клієнта Turso"""
    global client
    try:
        client = create_client(url=TURSO_URL, auth_token=TURSO_TOKEN)
        print("✅ Підключення до Turso успішне")
        return True
    except Exception as e:
        print(f"❌ Помилка підключення: {e}")
        return False

def get_db_client():
    """Отримати клієнта БД"""
    global client
    if client is None:
        init_client()
    return client

def init_db():
    """Ініціалізація таблиць БД"""
    try:
        db = get_db_client()
        if db is None:
            print("❌ Не вдалось підключитися до БД")
            return
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS trainers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        print("✅ База даних ініціалізована")
        
    except Exception as e:
        print(f"❌ Помилка ініціалізації БД: {e}")

# =========================
# 🏁 СТАРТ БОТА
# =========================

@bot.message_handler(commands=['start'])
def start(message):
    """Головне меню користувача"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
    
    bot.send_message(
        message.chat.id,
        "♟️ Ласкаво просимо до шахматної школи!\nВиберіть дію:",
        reply_markup=markup
    )
    user_states[message.chat.id] = "main_menu"

# =========================
# 👨‍💼 АДМІН-ПАНЕЛЬ
# =========================

@bot.message_handler(func=lambda message: message.text == "Edit")
def admin_panel(message):
    """Доступ до адмін-панелі (тільки для адміністратора)"""
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

# ===== ДОДАВАННЯ ТРЕНЕРА =====

@bot.message_handler(func=lambda message: message.text == "➕ Додати тренера")
def add_trainer_start(message):
    """Початок процесу додавання тренера"""
    if message.from_user.id != ADMIN_ID:
        return
    
    user_states[message.chat.id] = "waiting_trainer_username"
    bot.send_message(
        message.chat.id,
        "Введи @username тренера (з собачкою):\n(Приклад: @chess_coach_ivan)"
    )

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_username")
def get_trainer_username(message):
    """Отримання username тренера"""
    username = message.text.strip()
    
    if not username.startswith("@"):
        bot.send_message(message.chat.id, "❌ Username має починатися з @\nПопробуй ще раз:")
        return
    
    trainer_data[message.chat.id] = {"username": username}
    user_states[message.chat.id] = "waiting_trainer_name"
    bot.send_message(message.chat.id, "Введи ім'я тренера:")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_name")
def get_trainer_name(message):
    """Отримання імені тренера"""
    trainer_data[message.chat.id]["name"] = message.text
    user_states[message.chat.id] = "waiting_trainer_description"
    bot.send_message(message.chat.id, "Введи опис тренера (досвід, кваліфікація тощо):")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_trainer_description")
def get_trainer_description(message):
    """Отримання опису та збереження тренера в БД"""
    trainer_data[message.chat.id]["description"] = message.text
    data = trainer_data[message.chat.id]
    
    try:
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            return
        
        db.execute(
            "INSERT INTO trainers (username, name, description) VALUES (?, ?, ?)",
            [data["username"], data["name"], data["description"]]
        )
        
        bot.send_message(
            message.chat.id,
            f"✅ Тренер {data['name']} успішно додан!"
        )
        
    except Exception as e:
        error_str = str(e)
        if "UNIQUE constraint failed" in error_str or "unique" in error_str.lower():
            bot.send_message(
                message.chat.id,
                f"❌ Тренер з username {data['username']} уже існує"
            )
        else:
            bot.send_message(message.chat.id, f"❌ Помилка: {e}")
    
    user_states.pop(message.chat.id, None)
    trainer_data.pop(message.chat.id, None)

# ===== ВИДАЛЕННЯ ТРЕНЕРА =====

@bot.message_handler(func=lambda message: message.text == "➖ Видалити тренера")
def delete_trainer_start(message):
    """Показ списку тренерів для видалення"""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            return
        
        result = db.execute("SELECT id, name FROM trainers ORDER BY name")
        trainers = result.rows if hasattr(result, 'rows') else []
        
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
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_trainer_"))
def delete_trainer_confirm(call):
    """Видалення тренера"""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Немає доступу", show_alert=True)
        return
    
    trainer_id = call.data.split("_")[2]
    
    try:
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
        
        bot.answer_callback_query(call.id, "✅ Видалено!", show_alert=False)
        bot.edit_message_text(
            f"✅ Тренер '{trainer[0]}' видалений із системи",
            call.message.chat.id,
            call.message.message_id
        )
        
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Помилка: {e}", show_alert=True)

# ===== СПИСОК ТРЕНЕРІВ =====

@bot.message_handler(func=lambda message: message.text == "📋 Список тренерів")
def list_trainers(message):
    """Показ усіх тренерів"""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            return
        
        result = db.execute("SELECT id, name, username, description FROM trainers ORDER BY name")
        trainers = result.rows if hasattr(result, 'rows') else []
        
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
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

# =========================
# 👤 ВИБІР ТРЕНЕРА (КОРИСТУВАЧ)
# =========================

@bot.message_handler(func=lambda message: message.text == "Вибрати тренера")
def choose_trainer_start(message):
    """Початок процесу вибору тренера"""
    user_states[message.chat.id] = "waiting_phone"
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn = types.KeyboardButton("📱 Надіслати номер", request_contact=True)
    markup.add(btn)
    
    bot.send_message(
        message.chat.id,
        "Поділись своїм номером телефону:",
        reply_markup=markup
    )

@bot.message_handler(content_types=['contact'])
def get_phone(message):
    """Отримання номера телефону"""
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

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_user_name")
def get_user_name(message):
    """Отримання імені користувача"""
    if message.text == "◀️ Скасувати":
        cancel_selection(message)
        return
    
    user_form[message.chat.id]["name"] = message.text
    user_states[message.chat.id] = "waiting_level"
    
    bot.send_message(
        message.chat.id,
        "Опиши свій рівень гри в шахи:\n(Наприклад: Початківець, Середній, Продвинутий)"
    )

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_level")
def get_level(message):
    """Отримання рівня та показ списку тренерів"""
    if message.text == "◀️ Скасувати":
        cancel_selection(message)
        return
    
    user_form[message.chat.id]["level"] = message.text
    
    try:
        db = get_db_client()
        if db is None:
            bot.send_message(message.chat.id, "❌ Помилка підключення до БД")
            cancel_selection(message)
            return
        
        result = db.execute("SELECT id, name, description FROM trainers ORDER BY name")
        trainers = result.rows if hasattr(result, 'rows') else []
        
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
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")
        cancel_selection(message)

@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_trainer_"))
def send_request_to_trainer(call):
    """Надіслання заявки вибраному тренеру"""
    trainer_id = call.data.split("_")[2]
    
    try:
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
            bot.answer_callback_query(call.id, "✅ Заявка надіслана тренеру!", show_alert=False)
        except Exception as e:
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
        bot.answer_callback_query(call.id, f"❌ Помилка: {e}", show_alert=True)

def cancel_selection(message):
    """Скасування вибору тренера"""
    user_states.pop(message.chat.id, None)
    user_form.pop(message.chat.id, None)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Вибрати тренера", "Зв'язатися з адміністратором")
    
    bot.send_message(message.chat.id, "Скасовано. Головне меню:", reply_markup=markup)

# =========================
# 💬 ЧАТ З АДМІНІСТРАТОРОМ
# =========================

@bot.message_handler(func=lambda message: message.text == "Зв'язатися з адміністратором")
def contact_admin_start(message):
    """Ініціація чату з адміністратором"""
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_chat_"))
def accept_chat(call):
    """Адміністратор приймає чат"""
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
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_chat_"))
def reject_chat(call):
    """Адміністратор відхиляє чат"""
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
    except:
        pass

@bot.message_handler(func=lambda message: message.text == "🛑 Завершити чат")
def end_chat(message):
    """Завершення чату (користувач або адміністратор)"""
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

@bot.message_handler(func=lambda message: message.chat.id in admin_chats and user_states.get(message.chat.id) == "in_admin_chat")
def relay_user_message(message):
    """Пересилання повідомлення від користувача до адміна"""
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

@bot.message_handler(func=lambda message: message.from_user.id == ADMIN_ID)
def relay_admin_message(message):
    """Пересилання повідомлення від адміна до користувача"""
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
        print(f"❌ Помилка обробки webhook: {e}")
    return '', 200

@app.route('/health', methods=['GET'])
def health():
    """Health check для Render"""
    return 'OK', 200

# =========================
# 🚀 ЗАПУСК БОТА
# =========================

if __name__ == "__main__":
    print("🚀 Бот запускається...")
    init_db()
    
    # Видалити старий webhook
    try:
        bot.remove_webhook()
        print("✅ Старий webhook видалено")
    except:
        pass
    
    # Встановити новий webhook
    webhook_url = f"{WEBHOOK_URL}/webhook"
    try:
        bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook встановлено: {webhook_url}")
    except Exception as e:
        print(f"⚠️ Помилка встановлення webhook: {e}")
    
    # Запустити Flask додаток
    port = int(os.getenv("PORT", 5000))
    print(f"🌐 Запуск Flask на порту {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
