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

def get_user(user_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user


# --- FSM (СОСТОЯНИЯ) ---

# Регистрация
class Registration(StatesGroup):
    name = State()
    phone = State()

# Оформление заказа такси
class OrderTaxi(StatesGroup):
    from_address = State()  # Шаг 1: Откуда
    to_address = State()    # Шаг 2: Куда
    confirm = State()       # Шаг 3: Подтверждение


# --- КЛАВИАТУРЫ ---

# Главное меню
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚕 Заказать такси")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="📞 Поддержка")]
    ],
    resize_keyboard=True
)

# Кнопка отмены
cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

# Запрос номера телефона
phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

# Запрос геопозиции (Точка А)
location_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📍 Отправить мою геопозицию", request_location=True)],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

# Подтверждение заказа
confirm_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Подтвердить заказ")],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)


# --- ОБРАБОТЧИКИ КОМАНД И РЕГИСТРАЦИИ ---

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

@dp.message(F.text == "❌ Отмена")
@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять.")
        return

    await state.clear()
    user = get_user(message.from_user.id)
    reply_kb = main_menu if user else ReplyKeyboardRemove()
    await message.answer("Действие отменено.", reply_markup=reply_kb)

@dp.message(Command("register"))
async def start_register(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        await message.answer("Вы уже зарегистрированы!", reply_markup=main_menu)
        return

    await state.set_state(Registration.name)
    await message.answer("Как вас зовут?", reply_markup=cancel_keyboard)

@dp.message(Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(user_name=message.text)
    await state.set_state(Registration.phone)
    await message.answer(
        "Отлично! Нажмите кнопку ниже, чтобы поделиться номером телефона:",
        reply_markup=phone_keyboard
    )

@dp.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_name = user_data.get("user_name")

    if message.contact:
        user_phone = message.contact.phone_number
    else:
        user_phone = message.text

    save_user(message.from_user.id, user_name, user_phone)
    await state.clear()

    await message.answer(
        f"✅ Регистрация успешно завершена!\n\n"
        f"👤 Имя: {user_name}\n"
        f"📱 Телефон: {user_phone}",
        reply_markup=main_menu
    )


# --- ПРОФИЛЬ И ПОДДЕРЖКА ---

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


# --- СЦЕНАРИЙ ЗАКАЗА ТАКСИ ---

# 1. Начало заказа: Спрашиваем «Откуда»
@dp.message(F.text == "🚕 Заказать такси")
async def start_order(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала пройдите регистрацию с помощью команды /register")
        return

    await state.set_state(OrderTaxi.from_address)
    await message.answer(
        "🚕 **Заказ такси**\n\n"
        "Откуда вас забрать?\n"
        "Введите адрес текстом или нажмите кнопку **«📍 Отправить мою геопозицию»** ниже:",
        reply_markup=location_keyboard,
        parse_mode="Markdown"
    )

# 2. Обработка «Откуда» (Локация или Текст)
@dp.message(OrderTaxi.from_address)
async def process_from_address(message: types.Message, state: FSMContext):
    if message.location:
        # Если пришла локация, сохраняем координаты или ссылку на карты
        lat = message.location.latitude
        lon = message.location.longitude
        from_loc = f"📍 GPS: {lat:.5f}, {lon:.5f}"
    else:
        # Если пришел текст
        from_loc = message.text

    await state.update_data(from_address=from_loc)
    await state.set_state(OrderTaxi.to_address)

    await message.answer(
        "Куда едем?\nВведите адрес назначения текстом:",
        reply_markup=cancel_keyboard
    )

# 3. Обработка «Куда»
@dp.message(OrderTaxi.to_address)
async def process_to_address(message: types.Message, state: FSMContext):
    await state.update_data(to_address=message.text)
    await state.set_state(OrderTaxi.confirm)

    user_data = await state.get_data()
    from_addr = user_data.get("from_address")
    to_addr = user_data.get("to_address")
    
    user = get_user(message.from_user.id)
    name, phone = user

    # Показываем сводку заказа
    await message.answer(
        f"🚕 **Проверьте ваш заказ:**\n\n"
        f"👤 **Пассажир:** {name} ({phone})\n"
        f"🛫 **Откуда:** {from_addr}\n"
        f"🛬 **Куда:** {to_addr}\n\n"
        f"Все верно?",
        reply_markup=confirm_keyboard,
        parse_mode="Markdown"
    )

# 4. Подтверждение заказа
@dp.message(OrderTaxi.confirm, F.text == "✅ Подтвердить заказ")
async def process_confirm_order(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    from_addr = user_data.get("from_address")
    to_addr = user_data.get("to_address")
    user = get_user(message.from_user.id)
    name, phone = user

    await state.clear()

    # Сообщение клиенту
    await message.answer(
        "🎉 **Заказ принят!**\nИщем свободную машину. Водитель свяжется с вами в ближайшее время.",
        reply_markup=main_menu,
        parse_mode="Markdown"
    )

    # В БУДУЩЕМ: Здесь мы добавим отправку этого заказа водителям/в группу таксистов!


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
