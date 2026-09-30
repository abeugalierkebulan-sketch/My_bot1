import asyncio
import sqlite3
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

TOKEN = os.getenv("8685352650:AAGS495_9n2CEWkclEb4jC-9Hicl6RtGHgU")
ADMIN_ID = 1055896268  

bot = Bot(token=TOKEN)
dp = Dispatcher()

def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            name TEXT,
            age INTEGER
        )
    """)
    conn.commit()
    conn.close()

def save_user(user_id: int, name: str, age: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, name, age)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, age=excluded.age
    """, (user_id, name, age))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, age FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows

class Form(StatesGroup):
    name = State()
    age = State()
    broadcast_text = State()

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

@dp.message(F.text == "❌ Отмена")
@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())

@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)
    await message.answer("Привет! Начнем регистрацию.\nКак тебя зовут?", reply_markup=cancel_keyboard)

@dp.message(Form.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(Form.age)
    await message.answer(f"Отлично, {message.text}! Укажи возраст:", reply_markup=cancel_keyboard)

@dp.message(Form.age)
async def process_age(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите возраст числом!")
        return

    age = int(message.text)
    user_data = await state.get_data()
    await state.clear()

    save_user(
        user_id=message.from_user.id,
        name=user_data["name"],
        age=age
    )

    await message.answer(
        f"✅ **Данные успешно сохранены в базе!**\n\nИмя: {user_data['name']}\nВозраст: {age}",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("users"))
async def show_users(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    users = get_all_users()
    if not users:
        await message.answer("База данных пока пуста.")
        return

    response = "📋 **Зарегистрированные пользователи:**\n\n"
    for idx, (u_id, name, age) in enumerate(users, start=1):
        response += f"{idx}. {name} — {age} лет (ID: {u_id})\n"

    await message.answer(response)

@dp.message(Command("broadcast"))
async def start_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    await state.set_state(Form.broadcast_text)
    await message.answer(
        "📢 Введите текст сообщения для рассылки всем пользователям:",
        reply_markup=cancel_keyboard
    )

@dp.message(Form.broadcast_text)
async def perform_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    users = get_all_users()
    
    count = 0
    for u_id, name, age in users:
        try:
            await bot.send_message(
                chat_id=u_id,
                text=f"🔔 **Сообщение от администратора:**\n\n{message.text}"
            )
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(
        f"✅ Рассылка завершена!\nСообщение получили: **{count}** чел.",
        reply_markup=ReplyKeyboardRemove()
    )

async def main():
    init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
