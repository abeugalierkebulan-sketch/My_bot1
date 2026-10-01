import asyncio
import sqlite3
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# 1. Получаем токен из переменных окружения
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


# --- FSM (АНКЕТА) ---
class Registration(StatesGroup):
    name = State()   # Шаг 1: Имя
    phone = State()  # Шаг 2: Телефон


# --- КЛАВИАТУРЫ ---
cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

# Клавиатура с кнопкой запроса номера
phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)


# --- ОБРАБОТЧИКИ ---

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n"
        "Чтобы пройти регистрацию, напиши команду /register"
    )

# Отмена анкеты
@dp.message(F.text == "❌ Отмена")
@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять.", reply_markup=ReplyKeyboardRemove())
        return

    await state.clear()
    await message.answer("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())


# Старт анкеты
@dp.message(Command("register"))
async def start_register(message: types.Message, state: FSMContext):
    await state.set_state(Registration.name)
    await message.answer("Как вас зовут?", reply_markup=cancel_keyboard)


# Шаг 1: Ловим имя
@dp.message(Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(user_name=message.text)
    await state.set_state(Registration.phone)
    
    # Показываем клавиатуру с кнопкой "Поделиться номером"
    await message.answer(
        "Отлично! Нажмите кнопку ниже, чтобы поделиться номером телефона:",
        reply_markup=phone_keyboard
    )


# Шаг 2: Ловим телефон (как через кнопку контакта, так и обычным текстом)
@dp.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_name = user_data.get("user_name")

    # Проверяем: прислал ли пользователь контакт по кнопке или написал текстом
    if message.contact:
        user_phone = message.contact.phone_number
    else:
        user_phone = message.text

    # Сохраняем в SQLite
    save_user(message.from_user.id, user_name, user_phone)

    # Завершаем анкету
    await state.clear()

    await message.answer(
        f"✅ Регистрация успешно завершена!\n\n"
        f"👤 Имя: {user_name}\n"
        f"📱 Телефон: {user_phone}\n\n"
        f"Данные сохранены в базу SQLite.",
        reply_markup=ReplyKeyboardRemove()
    )


# --- ЗАПУСК БОТА ---
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
