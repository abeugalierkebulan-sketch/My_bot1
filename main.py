import asyncio
import sqlite3
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiohttp import web

TOKEN = os.getenv("BOT_TOKEN")

# Ваш действующий ID канала/группы
CHANNEL_ID = -1004421978587 

bot = Bot(token=TOKEN)
dp = Dispatcher()


def clean_phone(phone: str) -> str:
    cleaned = ''.join(filter(str.isdigit, str(phone)))
    if cleaned.startswith('8'):
        cleaned = '7' + cleaned[1:]
    elif not cleaned.startswith('7'):
        cleaned = '7' + cleaned
    return f"+{cleaned}"


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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passenger_id INTEGER,
            driver_id INTEGER DEFAULT NULL,
            from_addr TEXT,
            to_addr TEXT,
            status TEXT DEFAULT 'active'
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

def create_order(passenger_id: int, from_addr: str, to_addr: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO orders (passenger_id, from_addr, to_addr, status)
        VALUES (?, ?, ?, 'active')
    """, (passenger_id, from_addr, to_addr))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id


class Registration(StatesGroup):
    name = State()
    phone = State()

class OrderTaxi(StatesGroup):
    from_address = State()
    to_address = State()
    confirm = State()


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚕 Заказать такси")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="📞 Поддержка")]
    ],
    resize_keyboard=True
)

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

confirm_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Подтвердить заказ")],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)


@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user = get_user(message.from_user.id)
    if user:
        name, phone = user
        await message.answer(f"С возвращением, {name}! 👋", reply_markup=main_menu)
    else:
        await message.answer("Для работы с ботом пройдите регистрацию: напишите /register")

@dp.message(F.text == "❌ Отмена")
@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    reply_kb = main_menu if user else ReplyKeyboardRemove()
    await message.answer("Действие отменено.", reply_markup=reply_kb)

@dp.message(Command("register"))
async def start_register(message: types.Message, state: FSMContext):
    await state.set_state(Registration.name)
    await message.answer("Введите ваше имя:", reply_markup=cancel_keyboard)

@dp.message(Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(user_name=message.text)
    await state.set_state(Registration.phone)
    await message.answer("Введите ваш номер телефона (например: +77071234567):", reply_markup=cancel_keyboard)

@dp.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_name = user_data.get("user_name")
    user_phone = message.text

    save_user(message.from_user.id, user_name, user_phone)
    await state.clear()

    await message.answer(
        f"✅ Регистрация успешна!\n👤 Имя: {user_name}\n📱 Телефон: {user_phone}",
        reply_markup=main_menu
    )


@dp.message(F.text == "👤 Мой профиль")
async def show_profile(message: types.Message):
    user = get_user(message.from_user.id)
    if user:
        name, phone = user
        await message.answer(f"📋 Ваш профиль:\n\n👤 Имя: {name}\n📱 Телефон: {phone}")
    else:
        await message.answer("Вы не зарегистрированы. Напишите /register")

@dp.message(F.text == "📞 Поддержка")
async def show_support(message: types.Message):
    await message.answer("Служба поддержки: @support")


@dp.message(F.text == "🚕 Заказать такси")
async def start_order(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала пройдите регистрацию: /register")
        return

    await state.set_state(OrderTaxi.from_address)
    await message.answer("🚕 Откуда вас забрать? Напишите адрес:", reply_markup=cancel_keyboard)

@dp.message(OrderTaxi.from_address)
async def process_from_address(message: types.Message, state: FSMContext):
    await state.update_data(from_address=message.text)
    await state.set_state(OrderTaxi.to_address)
    await message.answer("Куда едем? Напишите адрес назначения:", reply_markup=cancel_keyboard)

@dp.message(OrderTaxi.to_address)
async def process_to_address(message: types.Message, state: FSMContext):
    await state.update_data(to_address=message.text)
    await state.set_state(OrderTaxi.confirm)

    user_data = await state.get_data()
    from_addr = user_data.get("from_address")
    to_addr = user_data.get("to_address")
    
    user = get_user(message.from_user.id)
    name, phone = user

    await message.answer(
        f"🚕 Проверьте заказ:\n\n"
        f"👤 Пассажир: {name} ({phone})\n"
        f"🛫 Откуда: {from_addr}\n"
        f"🛬 Куда: {to_addr}\n\n"
        f"Подтвердить?",
        reply_markup=confirm_keyboard
    )

@dp.message(OrderTaxi.confirm, F.text == "✅ Подтвердить заказ")
async def process_confirm_order(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    from_addr = user_data.get("from_address")
    to_addr = user_data.get("to_address")

    await state.clear()

    order_id = create_order(message.from_user.id, from_addr, to_addr)

    accept_button = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚕 Принять заказ", callback_data=f"accept_{order_id}")]
        ]
    )

    channel_order_text = (
        f"🚕 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
        f"🛫 Откуда: {from_addr}\n"
        f"🛬 Куда: {to_addr}\n\n"
        f"Нажмите кнопку ниже, чтобы забрать заказ:"
    )

    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=channel_order_text, reply_markup=accept_button)
        await message.answer("🎉 Заказ отправлен водителям!", reply_markup=main_menu)
    except Exception as e:
        await message.answer("Ошибка отправки заказа в группу.", reply_markup=main_menu)


# --- НАЖАТИЕ КНОПКИ «ПРИНЯТЬ ЗАКАЗ» ---

@dp.callback_query(F.data.startswith("accept_"))
async def handle_accept_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    driver_user_id = callback.from_user.id

    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT passenger_id, from_addr, to_addr FROM orders WHERE id = ?", (order_id,))
    order_data = cursor.fetchone()

    if not order_data:
        await callback.answer("Заказ не найден!", show_alert=True)
        conn.close()
        return

    passenger_id, from_addr, to_addr = order_data

    # Записываем водителя
    cursor.execute("UPDATE orders SET driver_id = ?, status = 'accepted' WHERE id = ?", (driver_user_id, order_id))
    conn.commit()
    conn.close()

    # Данные водителя и пассажира
    driver_info = get_user(driver_user_id)
    driver_name = driver_info[0] if driver_info else (callback.from_user.first_name or "Водитель")
    driver_phone = driver_info[1] if driver_info else "Не указан"

    passenger_info = get_user(passenger_id)
    pass_name = passenger_info[0] if passenger_info else "Пассажир"
    pass_phone = passenger_info[1] if passenger_info else "Не указан"

    clean_pass_phone = clean_phone(pass_phone)
    clean_driver_phone = clean_phone(driver_phone)

    await callback.answer("Вы приняли заказ!")

    # 1. Изменяем текст в группе
    try:
        await callback.message.edit_text(
            text=f"✅ ЗАКАЗ #{order_id} ПРИНЯТ\n\n🛫 Откуда: {from_addr}\n🛬 Куда: {to_addr}\n\n🚕 Водитель: {driver_name}",
            reply_markup=None
        )
    except Exception as e:
        print(f"Ошибка в группе: {e}")

    # 2. Карточка ВОДИТЕЛЮ в ЛС
    try:
        driver_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📞 Позвонить клиенту", url=f"tel:{clean_pass_phone}")],
            [InlineKeyboardButton(text="💬 WhatsApp клиенту", url=f"https://wa.me/{clean_pass_phone.replace('+', '')}")]
        ])
        driver_msg = (
            f"— ЖАҢА ТАПСЫРЫС —\n\n"
            f"Тапсырыс № {order_id}\n\n"
            f"👤 Клиент: {pass_name}\n"
            f"🛫 Қайдан: {from_addr}\n"
            f"🛬 Қайда: {to_addr}\n"
            f"📱 Телефон: {clean_pass_phone}\n\n"
            f"Қабылдады: {driver_name}"
        )
        await bot.send_message(chat_id=driver_user_id, text=driver_msg, reply_markup=driver_kb)
    except Exception as e:
        print(f"ОШИБКА ОТПРАВКИ ВОДИТЕЛЮ: {e}")

    # 3. Карточка ПАССАЖИРУ в ЛС
    try:
        passenger_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📞 Позвонить водителю", url=f"tel:{clean_driver_phone}")],
            [InlineKeyboardButton(text="💬 WhatsApp водителю", url=f"https://wa.me/{clean_driver_phone.replace('+', '')}")]
        ])
        passenger_msg = (
            f"🚖 Ваш заказ № {order_id} принят!\n\n"
            f"👤 Водитель: {driver_name}\n"
            f"📱 Телефон: {clean_driver_phone}\n\n"
            f"Водитель свяжется с вами."
        )
        await bot.send_message(chat_id=passenger_id, text=passenger_msg, reply_markup=passenger_kb)
    except Exception as e:
        print(f"ОШИБКА ОТПРАВКИ ПАССАЖИРУ: {e}")


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

async def main():
    init_db()
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
