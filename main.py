import os
import sqlite3
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ChatJoinRequest

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

CITY_GROUP_ID = int(os.getenv("CITY_GROUP_ID", "-1001234567890"))
INTERCITY_GROUP_ID = int(os.getenv("INTERCITY_GROUP_ID", "-1000987654321"))

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            order_type TEXT,
            from_loc TEXT,
            to_loc TEXT,
            date_time TEXT,
            seats TEXT,
            price TEXT,
            phone TEXT,
            status TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS drivers (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            car_info TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- FSM (СОСТОЯНИЯ) ---
class OrderCity(StatesGroup):
    from_loc = State()
    to_loc = State()
    price = State()
    phone = State()

class OrderIntercity(StatesGroup):
    from_loc = State()
    to_loc = State()
    date_time = State()
    seats = State()
    price = State()
    phone = State()

class DriverRegister(StatesGroup):
    full_name = State()
    phone = State()
    car_info = State()

# --- МЕНЮ ---
def main_menu():
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙 Қала ішінде"), KeyboardButton(text="🛣 Қалааралық (Межгород)")],
            [KeyboardButton(text="🚖 Жүргізуші болу"), KeyboardButton(text="📞 Қолдау қызметі")]
        ],
        resize_keyboard=True
    )
    return kb

def cancel_menu():
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Бас тарту")]],
        resize_keyboard=True
    )
    return kb

# --- START ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        f"Ассалаумағалейкум, <b>{message.from_user.first_name}</b>!\n\n"
        f"🚖 <b>«Жолдас такси»</b> ботына қош келдіңіз!\n\n"
        f"Бізбен бірге қала ішінде немесе қалааралық сапарларға тез әрі ынғайлы тапсырыс бере аласыз.\n\n"
        f"Керекті бөлімді таңдаңыз 👇"
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")

@dp.message(F.text == "❌ Бас тарту")
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Тоқтатылды.", reply_markup=main_menu())

# ==========================================
# 🚖 РЕГИСТРАЦИЯ ВОДИТЕЛЯ (ЖҮРГІЗҮШІ БОЛУ)
# ==========================================
@dp.message(F.text == "🚖 Жүргізуші болу")
async def start_driver_reg(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drivers WHERE user_id = ?", (message.from_user.id,))
    driver = cursor.fetchone()
    conn.close()

    if driver:
        await message.answer("✅ <b>Сіз тіркелген жүргізушісіз!</b> Топтарға өту үшін өтінім жіберсеңіз, бот сізді автоматты түрде қабылдайды.", parse_mode="HTML")
        return

    await state.set_state(DriverRegister.full_name)
    await message.answer("📝 <b>Жүргізуші болып тіркелу</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(DriverRegister.full_name)
async def process_driver_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await state.set_state(DriverRegister.phone)
    
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Нөмірді жіберу", request_contact=True)],
            [KeyboardButton(text="❌ Бас тарту")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("📱 Байланыс телефоныңызды жіберіңіз:", reply_markup=phone_kb)

@dp.message(DriverRegister.phone)
async def process_driver_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    await state.set_state(DriverRegister.car_info)
    await message.answer("🚘 Көлігіңіздің маркасы мен мемлекеттік нөмірін жазыңыз\n(мысалы: Toyota Camry 70, 777 AAA 02):", reply_markup=cancel_menu())

@dp.message(DriverRegister.car_info)
async def process_driver_car(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO drivers (user_id, full_name, phone, car_info)
        VALUES (?, ?, ?, ?)
    ''', (message.from_user.id, data['full_name'], data['phone'], message.text))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(
        "🎉 <b>Құттықтаймыз! Сіз «Жолдас такси» жүргізушісі ретінде тіркелдіңіз!</b>\n\n"
        "Енді жұмыс топтарына қосылу үшін өтінім жіберіңіз. Бот сізді автоматты түрде қабылдайды!",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

# ==========================================
# 🛡 АВТО-ОДОБРЕНИЕ ЗАЯВОК В ГРУППЫ
# ==========================================
@dp.chat_join_request()
async def auto_approve_driver(chat_join_request: ChatJoinRequest):
    user_id = chat_join_request.from_user.id
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drivers WHERE user_id = ?", (user_id,))
    driver = cursor.fetchone()
    conn.close()

    if driver:
        await chat_join_request.approve()
        await bot.send_message(
            chat_id=user_id,
            text="✅ <b>Жолдас такси:</b> Жұмыс тобына қосылу өтінішіңіз автоматты түрде қабылданды! Сәтті сапар!",
            parse_mode="HTML"
        )
    else:
        await chat_join_request.decline()
        await bot.send_message(
            chat_id=user_id,
            text="❌ <b>Топқа кіруге рұқсат берілмеді!</b>\n\nБұл топ тек тіркелген жүргізушілерге арналған. Алдымен ботта <b>«🚖 Жүргізуші болу»</b> батырмасын басып, тіркеліңіз!",
            parse_mode="HTML"
        )

# ==========================================
# 🏙 1. ҚАЛА ІШІНДЕ (ПО ГОРОДУ)
# ==========================================
@dp.message(F.text == "🏙 Қала ішінде")
async def start_city_order(message: types.Message, state: FSMContext):
    await state.set_state(OrderCity.from_loc)
    await message.answer("📍 <b>Қайдан алып кетейік?</b>\n(Көше, үй номері немесе ғимарат атауын жазыңыз):", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderCity.from_loc)
async def process_city_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderCity.to_loc)
    await message.answer("🏁 <b>Қайда барамыз?</b>:", parse_mode="HTML")

@dp.message(OrderCity.to_loc)
async def process_city_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    await state.set_state(OrderCity.price)
    await message.answer("💰 <b>Жолқыны канша ұсынасыз?</b> (мысалы: 1000 ₸):", parse_mode="HTML")

@dp.message(OrderCity.price)
async def process_city_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    await state.set_state(OrderCity.phone)
    
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Нөмірді жіберу", request_contact=True)],
            [KeyboardButton(text="❌ Бас тарту")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("📱 <b>Байланыс телефоныңызды жіберіңіз:</b>", reply_markup=phone_kb, parse_mode="HTML")

@dp.message(OrderCity.phone)
async def process_city_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO orders (user_id, order_type, from_loc, to_loc, price, phone, status)
        VALUES (?, 'city', ?, ?, ?, ?, 'new')
    ''', (message.from_user.id, data['from_loc'], data['to_loc'], data['price'], phone))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Тапсырысыңыз қабылданды!</b> Жүргізуші іздестірілуде...", reply_markup=main_menu(), parse_mode="HTML")

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛА ІШІНДЕ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {data['from_loc']}\n"
        f"🏁 <b>Қайда:</b> {data['to_loc']}\n"
        f"💰 <b>Ұсынған бағасы:</b> {data['price']}\n"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]
    ])
    
    await bot.send_message(chat_id=CITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# ==========================================
# 🛣 2. ҚАЛААРАЛЫҚ (МЕЖГОРОД)
# ==========================================
@dp.message(F.text == "🛣 Қалааралық (Межгород)")
async def start_intercity_order(message: types.Message, state: FSMContext):
    await state.set_state(OrderIntercity.from_loc)
    await message.answer("📍 <b>Қай қаладан / ауылдан шығасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderIntercity.from_loc)
async def process_inter_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderIntercity.to_loc)
    await message.answer("🏁 <b>Қай қалаға / ауылға барасыз?</b>", parse_mode="HTML")

@dp.message(OrderIntercity.to_loc)
async def process_inter_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    await state.set_state(OrderIntercity.date_time)
    await message.answer("📅 <b>Қай күні және сағат каншада?</b>\n(мысалы: Бүгін сағат 18:00-де):", parse_mode="HTML")

@dp.message(OrderIntercity.date_time)
async def process_inter_time(message: types.Message, state: FSMContext):
    await state.update_data(date_time=message.text)
    await state.set_state(OrderIntercity.seats)
    await message.answer("👥 <b>Қанша орын керек немесе багаж ба?</b>\n(мысалы: 2 адам / немесе бос багаж):", parse_mode="HTML")

@dp.message(OrderIntercity.seats)
async def process_inter_seats(message: types.Message, state: FSMContext):
    await state.update_data(seats=message.text)
    await state.set_state(OrderIntercity.price)
    await message.answer("💰 <b>Бір орынға қанша жолқы ұсынасыз?</b>\n(мысалы: 4 000 ₸):", parse_mode="HTML")

@dp.message(OrderIntercity.price)
async def process_inter_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    await state.set_state(OrderIntercity.phone)

    phone_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Нөмірді жіберу", request_contact=True)],
            [KeyboardButton(text="❌ Бас тарту")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("📱 <b>Байланыс телефоныңызды жіберіңіз:</b>", reply_markup=phone_kb, parse_mode="HTML")

@dp.message(OrderIntercity.phone)
async def process_inter_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO orders (user_id, order_type, from_loc, to_loc, date_time, seats, price, phone, status)
        VALUES (?, 'intercity', ?, ?, ?, ?, ?, ?, 'new')
    ''', (message.from_user.id, data['from_loc'], data['to_loc'], data['date_time'], data['seats'], data['price'], phone))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Қалааралық тапсырысыңыз қабылданды!</b> Жүргізушілерге жіберілді.", reply_markup=main_menu(), parse_mode="HTML")

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛААРАЛЫҚ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {data['from_loc']}\n"
        f"🏁 <b>Қайда:</b> {data['to_loc']}\n"
        f"📅 <b>Уақыты:</b> {data['date_time']}\n"
        f"👥 <b>Орын / Адам:</b> {data['seats']}\n"
        f"💰 <b>Ұсынған бағасы:</b> {data['price']}\n"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]
    ])
    
    await bot.send_message(chat_id=INTERCITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# ==========================================
# 🚖 ПРИНЯТИЕ ЗАКАЗА
# ==========================================
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback_query: types.CallbackQuery):
    order_id = callback_query.data.split('_')[1]
    driver = callback_query.from_user
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, order_type, from_loc, to_loc, price, phone, status FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()

    if not order:
        await callback_query.answer("Тапсырыс табылмады!", show_alert=True)
        conn.close()
        return

    if order[6] != 'new':
        await callback_query.answer("Өкінішке орай, бұл тапсырысты басқа жүргізуші алып қойды!", show_alert=True)
        conn.close()
        return

    cursor.execute("UPDATE orders SET status = 'accepted' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    client_id, order_type, from_loc, to_loc, price, client_phone = order[0], order[1], order[2], order[3], order[4], order[5]

    await callback_query.message.edit_text(
        f"{callback_query.message.text}\n\n✅ <b>ТАПСЫРЫС АЛЫНДЫ!</b>\nЖүргізуші: {driver.full_name} (@{driver.username})",
        parse_mode="HTML"
    )

    clean_phone = ''.join(filter(str.isdigit, client_phone))
    driver_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 WhatsApp-пен жазу", url=f"https://wa.me/{clean_phone}"),
            InlineKeyboardButton(text="📞 Қоңырау шалу", url=f"tel:+{clean_phone}")
        ]
    ])
    
    await bot.send_message(
        chat_id=driver.id,
        text=f"✅ <b>Сіз Жолдас такси №{order_id} тапсырысын қабылдадыңыз!</b>\n\n📍 <b>Маршрут:</b> {from_loc} ➔ {to_loc}\n💰 <b>Бағасы:</b> {price}\n📱 <b>Клиент нөмірі:</b> {client_phone}",
        reply_markup=driver_kb,
        parse_mode="HTML"
    )

    driver_username = f"@{driver.username}" if driver.username else "Жоқ"
    await bot.send_message(
        chat_id=client_id,
        text=f"🚖 <b>Жолдас такси: №{order_id} тапсырысыңызды жүргізуші қабылдады!</b>\n\n<b>Жүргізуші:</b> {driver.full_name}\n<b>Telegram:</b> {driver_username}\n\nЖүргізуші жақын арада сізбен байланысады.",
        parse_mode="HTML"
    )

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
