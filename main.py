import asyncio
import sqlite3
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiohttp import web

# 1. Токен из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- БАЗА ДАННЫХ (SQLite) ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            name TEXT,
            phone TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_user(user_id: int, name: str, phone: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, name, phone)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, phone=excluded.phone
    """, (user_id, name, phone))
    conn.commit()
    conn.close()

# Функция получения данных пользователя из базы
def get_user(user_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user  # Вернет tuple (name, phone) или None


# --- FSM (АНКЕТА РЕГИСТРАЦИИ) ---
class Registration(StatesGroup):
    name = State()   # Шаг 1: Имя
    phone = State()  # Шаг 2: Телефон


# --- КЛАВИАТУРЫ ---

# 1. Главное меню для зарегистрированных пользователей
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚕 Заказать такси")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="📞 Поддержка")]
    ],
    resize_keyboard=True
)

# 2. Кнопка отмены анкеты
cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

# 3. Кнопка запроса номера
phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)


# --- ОБРАБОТЧИКИ КОМАНД ---

# Старт с проверкой регистрации
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user = get_user(message.from_user.id)
    
    if user:
        name, phone = user
        await message.answer(
            f"С возвращением, {name}! 👋\nВыберите действие из меню ниже:",
            reply_markup=main_menu
        )
    else:
        await message.answer(
            f"Привет, {message.from_user.first_name}! 👋\n\n"
            "Вы еще не зарегистрированы в сервисе такси.\n"
            "Напишите команду /register чтобы пройти быструю регистрацию."
        )

# Отмена анкеты
@dp.message(F.text == "❌ Отмена")
@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять.")
        return

    await state.clear()
    
    # Если пользователь зарегистрирован, возвращаем в Главное меню
    user = get_user(message.from_user.id)
    reply_kb = main_menu if user else ReplyKeyboardRemove()
    
    await message.answer("Регистрация отменена.", reply_markup=reply_kb)

# Старт анкеты
@dp.message(Command("register"))
async def start_register(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        await message.answer("Вы уже зарегистрированы!", reply_markup=main_menu)
        return

    await state.set_state(Registration.name)
    await message.answer("Как вас зовут?", reply_markup=cancel_keyboard)

# Шаг 1: Имя
@dp.message(Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(user_name=message.text)
    await state.set_state(Registration.phone)
    await message.answer(
        "Отлично! Нажмите кнопку ниже, чтобы поделиться номером телефона:",
        reply_markup=phone_keyboard
    )

# Шаг 2: Телефон
@dp.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_name = user_data.get("user_name")

    if message.contact:
        user_phone = message.contact.phone_number
    else:
        user_phone = message.text

    # Сохраняем в SQLite
    save_user(message.from_user.id, user_name, user_phone)
    await state.clear()

    await message.answer(
        f"✅ Регистрация успешно завершена!\n\n"
        f"👤 Имя: {user_name}\n"
        f"📱 Телефон: {user_phone}",
        reply_markup=main_menu
    )


# --- ОБРАБОТКА КНОПОК ГЛАВНОГО МЕНЮ ---

@dp.message(F.text == "👤 Мой профиль")
async def show_profile(message: types.Message):
    user = get_user(message.from_user.id)
    if user:
        name, phone = user
        await message.answer(
            f"📋 **Ваш профиль:**\n\n"
            f"👤 Имя: {name}\n"
            f"📱 Телефон: {phone}\n"
            f"🆔 ID: `{message.from_user.id}`",
            parse_mode="Markdown"
        )
    else:
        await message.answer("Вы не зарегистрированы. Напишите /register")

@dp.message(F.text == "📞 Поддержка")
async def show_support(message: types.Message):
    await message.answer("Служба поддержки: @your_support_username\nТелефон: +7 (777) 000-00-00")

@dp.message(F.text == "🚕 Заказать такси")
async def start_order(message: types.Message):
    await message.answer("🚕 Скоро здесь будет система оформления заказа такси (укажите откуда и куда)!")


# --- ФЕЙКОВЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# --- ЗАПУСК ---
async def main():
    init_db()
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
