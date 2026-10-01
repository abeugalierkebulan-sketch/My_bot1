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

# ID вашей группы
CHANNEL_ID = -1004421078507 

bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_phone(phone: str) -> str:
    """Форматирует номер телефона в международный формат +7XXXXXXXXXX."""
    cleaned = ''.join(filter(str.isdigit, str(phone)))
    if cleaned.startswith('8'):
        cleaned = '7' + cleaned[1:]
    elif not cleaned.startswith('7'):
        cleaned = '7' + cleaned
    return f"+{cleaned}"


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

def assign_order_to_driver(order_id: int, driver_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, driver_id FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()
    
    if not order:
        conn.close()
        return "not_found"
        
    status, current_driver = order
    if status == 'active':
        cursor.execute("""
            UPDATE orders SET driver_id = ?, status = 'accepted' WHERE id = ?
        """, (driver_id, order_id))
        conn.commit()
        conn.close()
        return "success"
    elif current_driver == driver_id:
        conn.close()
        return "already_yours"
    else:
        conn.close()
        return "taken_by_other"


# --- FSM (СОСТОЯНИЯ) ---
class Registration(StatesGroup):
    name = State()
    phone = State()

class OrderTaxi(StatesGroup):
    from_address = State()
    to_address = State()
    confirm = State()


# --- КЛАВИАТУРЫ ---
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

phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

location_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📍 Отправить мою геопозицию", request_location=True)],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

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
            f"📋 Ваш профиль:\n\n"
            f"👤 Имя: {name}\n"
            f"📱 Телефон: {phone}\n"
            f"🆔 ID: {message.from_user.id}"
        )
    else:
        await message.answer("Вы не зарегистрированы. Напишите /register")

@dp.message(F.text == "📞 Поддержка")
async def show_support(message: types.Message):
    await message.answer("Служба поддержки: @your_support_username\nТелефон: +7 (777) 000-00-00")


# --- СЦЕНАРИЙ ЗАКАЗА ТАКСИ ---

@dp.message(F.text == "🚕 Заказать такси")
async def start_order(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала пройдите регистрацию с помощью команды /register")
        return

    await state.set_state(OrderTaxi.from_address)
    await message.answer(
        "🚕 Заказ такси\n\n"
        "Откуда вас забрать?\n"
        "Введите адрес текстом или нажмите кнопку «📍 Отправить мою геопозицию» ниже:",
        reply_markup=location_keyboard
    )

@dp.message(OrderTaxi.from_address)
async def process_from_address(message: types.Message, state: FSMContext):
    if message.location:
        lat = message.location.latitude
        lon = message.location.longitude
        from_loc = f"Геопозиция: https://yandex.ru/maps/?pt={lon},{lat}&z=16&l=map"
    else:
        from_loc = message.text

    await state.update_data(from_address=from_loc)
    await state.set_state(OrderTaxi.to_address)

    await message.answer(
        "Куда едем?\nВведите адрес назначения текстом:",
        reply_markup=cancel_keyboard
    )

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
        f"🚕 Проверьте ваш заказ:\n\n"
        f"👤 Пассажир: {name} ({phone})\n"
        f"🛫 Откуда: {from_addr}\n"
        f"🛬 Куда: {to_addr}\n\n"
        f"Все верно?",
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
            [InlineKeyboardButton(text="🚕 Принять заказ", callback_data=f"accept_order_{order_id}")]
        ]
    )

    channel_order_text = (
        f"🚕 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
        f"🛫 Откуда: {from_addr}\n"
        f"🛬 Куда: {to_addr}\n\n"
        f"👇 Нажмите кнопку ниже, чтобы забрать заказ:"
    )

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=channel_order_text,
            reply_markup=accept_button,
            disable_web_page_preview=True
        )
        
        await message.answer(
            "🎉 Ваш заказ успешно отправлен водителям!\nКак только водитель примет заказ, вы получите уведомление.",
            reply_markup=main_menu
        )
    except Exception as e:
        await message.answer(
            "⚠️ Ошибка при отправке заказа. Проверьте настройки группы.",
            reply_markup=main_menu
        )
        print(f"Ошибка отправки в группу: {e}")


# --- ОБРАБОТКА НАЖАТИЯ КНОПКИ «ПРИНЯТЬ ЗАКАЗ» ---

@dp.callback_query(F.data.startswith("accept_order_"))
async def handle_accept_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    driver_user_id = callback.from_user.id

    driver_info = get_user(driver_user_id)
    if driver_info:
        driver_name, driver_phone = driver_info
    else:
        driver_name = callback.from_user.first_name or "Водитель"
        driver_phone = "Не указан"

    result = assign_order_to_driver(order_id, driver_user_id)

    if result in ["success", "already_yours"]:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT passenger_id, from_addr, to_addr FROM orders WHERE id = ?", (order_id,))
        order_data = cursor.fetchone()
        conn.close()

        passenger_id, from_addr, to_addr = order_data
        passenger_info = get_user(passenger_id)
        pass_name, pass_phone = passenger_info if passenger_info else ("Пассажир", "Не указан")

        clean_pass_phone = clean_phone(pass_phone)
        clean_driver_phone = clean_phone(driver_phone)
        
        # Убираем знак плюс для рабочей ссылки WhatsApp
        wa_pass_phone = clean_pass_phone.replace("+", "")
        wa_driver_phone = clean_driver_phone.replace("+", "")

        # 1. Простое тихое подтверждение кнопки (без всплывающего окошка с текстом)
        await callback.answer("Заказ принят!")

        # 2. Обновляем пост в группе
        order_accepted_text = (
            f"✅ ЗАКАЗ #{order_id} ПРИНЯТ\n\n"
            f"🛫 Откуда: {from_addr}\n"
            f"🛬 Куда: {to_addr}\n\n"
            f"🚕 Водитель: {driver_name}"
        )
        try:
            await callback.message.edit_text(
                text=order_accepted_text,
                reply_markup=None,
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Ошибка при изменении сообщения в группе: {e}")

        # 3. Карточка ВОДИТЕЛЮ в личные сообщения (как на скриншоте)
        try:
            driver_contact_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📞 Позвонить", url=f"tel:{clean_pass_phone}")],
                    [InlineKeyboardButton(text="💬 WhatsApp", url=f"https://wa.me/{wa_pass_phone}")]
                ]
            )
            driver_card_text = (
                f"— ЖАҢА ТАПСЫРЫС —\n\n"
                f"Тапсырыс № {order_id}\n\n"
                f"Қайдан: {from_addr}\n"
                f"Қайда: {to_addr}\n"
                f"Телефон: {clean_pass_phone}\n\n"
                f"Қабылдады: {driver_name}"
            )
            await bot.send_message(
                chat_id=driver_user_id,
                text=driver_card_text,
                reply_markup=driver_contact_markup,
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Ошибка отправки водителю в ЛС: {e}")

        # 4. Карточка ПАССАЖИРУ в личные сообщения
        try:
            passenger_contact_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📞 Позвонить водителю", url=f"tel:{clean_driver_phone}")],
                    [InlineKeyboardButton(text="💬 WhatsApp водителю", url=f"https://wa.me/{wa_driver_phone}")]
                ]
            )
            passenger_card_text = (
                f"🚖 Ваш заказ № {order_id} принят!\n\n"
                f"👤 Водитель: {driver_name}\n"
                f"📱 Телефон: {clean_driver_phone}\n\n"
                f"Водитель свяжется с вами в ближайшее время."
            )
            await bot.send_message(
                chat_id=passenger_id,
                text=passenger_card_text,
                reply_markup=passenger_contact_markup,
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Ошибка отправки пассажиру в ЛС: {e}")

    else:
        await callback.answer("❌ Этот заказ уже принял другой водитель!", show_alert=True)


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

