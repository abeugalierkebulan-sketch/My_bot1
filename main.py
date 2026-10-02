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
from aiohttp import web

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("BOT_TOKEN")

CITY_GROUP_ID = int(os.getenv("CITY_GROUP_ID", "-1004350443552"))
INTERCITY_GROUP_ID = int(os.getenv("INTERCITY_GROUP_ID", "-1003756709241"))

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
            delivery_type TEXT,
            item_info TEXT,
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT
        )
    ''')

    conn.commit()
    conn.close()

init_db()

# --- FSM (СОСТОЯНИЯ) ---
class ClientRegister(StatesGroup):
    full_name = State()
    phone = State()

class OrderCity(StatesGroup):
    from_loc = State()
    to_loc = State()
    price = State()

class OrderIntercity(StatesGroup):
    from_loc = State()
    to_loc = State()
    date_time = State()
    seats = State()
    price = State()

class OrderDelivery(StatesGroup):
    delivery_type = State() # 'city' или 'intercity'
    item_info = State()     # Что именно доставить
    from_loc = State()      # Откуда забрать
    to_loc = State()        # Куда доставить
    price = State()         # Цена

class DriverRegister(StatesGroup):
    full_name = State()
    phone = State()
    car_info = State()

# --- МЕНЮ ---
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙 Қала ішінде"), KeyboardButton(text="🛣 Қалааралық (Межгород)")],
            [KeyboardButton(text="📦 Жеткізу (Доставка)"), KeyboardButton(text="🚖 Жүргізуші болу")],
            [KeyboardButton(text="📞 Қолдау қызметі")]
        ],
        resize_keyboard=True
    )

def delivery_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙 Қала ішінде"), KeyboardButton(text="🛣 Қалааралық (Межгород)")],
            [KeyboardButton(text="❌ Бас тарту")]
        ],
        resize_keyboard=True
    )

def cancel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Бас тарту")]],
        resize_keyboard=True
    )

async def is_client_registered(user_id: int) -> bool:
    try:
        conn = sqlite3.connect("joldas_taxi.db")
        cursor = conn.cursor()
        cursor.execute("SELECT full_name FROM clients WHERE user_id = ?", (user_id,))
        client = cursor.fetchone()
        conn.close()
        return client is not None
    except Exception as e:
        logging.error(f"Ошибка проверки клиента: {e}")
        return False

def clean_phone_number(raw_phone: str) -> str:
    digits = ''.join(filter(str.isdigit, str(raw_phone)))
    if len(digits) == 10:
        return "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits

# --- СТАРТ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_client_registered(message.from_user.id):
        text = (
            f"Ассалаумағалейкум, <b>{message.from_user.first_name}</b>!\n\n"
            f"🚖 <b>«Жолдас такси»</b> ботына қош келдіңіз!\n\n"
            f"Керекті бөлімді таңдаңыз 👇"
        )
        await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")
    else:
        await state.set_state(ClientRegister.full_name)
        text = (
            f"Ассалаумағалейкум, <b>{message.from_user.first_name}</b>!\n\n"
            f"🚖 <b>«Жолдас такси»</b> қызметін пайдалану үшін алдымен тіркелу қажет.\n\n"
            f"👤 <b>Аты-жөніңізді жазыңыз (ФИО):</b>"
        )
        await message.answer(text, reply_markup=cancel_menu(), parse_mode="HTML")

# --- ОТМЕНА ---
@dp.message(F.text.in_({"❌ Бас тарту", "⬅️ Қайту (артқа)"}))
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_client_registered(message.from_user.id):
        await message.answer("❌ Тапсырыс тоқтатылды.", reply_markup=main_menu())
    else:
        await message.answer("❌ Тоқтатылды. Қайта бастау үшін /start басыңыз.", reply_markup=types.ReplyKeyboardRemove())

# --- РЕГИСТРАЦИЯ КЛИЕНТА ---
@dp.message(ClientRegister.full_name)
async def process_client_name(message: types.Message, state: FSMContext):
    await state.update_data(client_full_name=message.text)
    await state.set_state(ClientRegister.phone)
    await message.answer("📱 Байланыс телефоныңызды енгізіңіз (мысалы: 87071234567):", reply_markup=cancel_menu())

@dp.message(ClientRegister.phone)
async def process_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    phone = message.text
    try:
        conn = sqlite3.connect("joldas_taxi.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO clients (user_id, full_name, phone) VALUES (?, ?, ?)",
                       (message.from_user.id, data.get('client_full_name', 'Клиент'), phone))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка сохранения клиента: {e}")

    await state.clear()
    await message.answer("✅ <b>Сіз сәтті тіркелдіңіз!</b>\n\nЕнді керекті бөлімді таңдай аласыз 👇", reply_markup=main_menu(), parse_mode="HTML")

# --- РЕГИСТРАЦИЯ ВОДИТЕЛЯ ---
@dp.message(F.text == "🚖 Жүргізуші болу")
async def start_driver_reg(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drivers WHERE user_id = ?", (message.from_user.id,))
    driver = cursor.fetchone()
    conn.close()

    if driver:
        try:
            city_link = await bot.export_chat_invite_link(CITY_GROUP_ID)
            intercity_link = await bot.export_chat_invite_link(INTERCITY_GROUP_ID)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏙 Қала ішіндегі топқа қосылу", url=city_link)],
                [InlineKeyboardButton(text="🛣 Қалааралық топқа қосылу", url=intercity_link)]
            ])
            await message.answer("✅ <b>Сіз тіркелген жүргізушісіз!</b> Топтарға өту үшін төмендегі сілтемелерді басыңыз:", reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка получения ссылок: {e}")
            await message.answer("✅ <b>Сіз тіркелген жүргізушісіз!</b> Жұмыс топтарына қосылыңыз.", parse_mode="HTML")
        return

    await state.set_state(DriverRegister.full_name)
    await message.answer("📝 <b>Жүргізуші болып тіркелу</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(DriverRegister.full_name)
async def process_driver_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await state.set_state(DriverRegister.phone)
    await message.answer("📱 Байланыс телефоныңызды енгізіңіз (мысалы: 87071234567):", reply_markup=cancel_menu())

@dp.message(DriverRegister.phone)
async def process_driver_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(DriverRegister.car_info)
    await message.answer("🚘 Көлігіңіздің маркасы мен мемлекеттік нөмірін жазыңыз:", reply_markup=cancel_menu())

@dp.message(DriverRegister.car_info)
async def process_driver_car(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO drivers (user_id, full_name, phone, car_info) VALUES (?, ?, ?, ?)',
                   (message.from_user.id, data['full_name'], data['phone'], message.text))
    conn.commit()
    conn.close()
    await state.clear()

    try:
        city_link = await bot.export_chat_invite_link(CITY_GROUP_ID)
        intercity_link = await bot.export_chat_invite_link(INTERCITY_GROUP_ID)
        group_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏙 Қала ішіндегі топқа қосылу", url=city_link)],
            [InlineKeyboardButton(text="🛣 Қалааралық топқа қосылу", url=intercity_link)]
        ])
        await message.answer("🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b>\n\nТоптарға қосылыңыз 👇", reply_markup=group_kb, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка сгенерировать ссылки: {e}")
        await message.answer("🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b>", reply_markup=main_menu(), parse_mode="HTML")

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
    else:
        await chat_join_request.decline()

# --- 1. ЗАКАЗ ПО ГОРОДУ ---
@dp.message(F.text == "🏙 Қала ішінде")
async def start_city_order(message: types.Message, state: FSMContext):
    if not await is_client_registered(message.from_user.id):
        await state.set_state(ClientRegister.full_name)
        await message.answer("👤 <b>Тапсырыс беру үшін алдымен тіркелу қажет!</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):", reply_markup=cancel_menu(), parse_mode="HTML")
        return
    await state.set_state(OrderCity.from_loc)
    await message.answer("📍 <b>Қайдан алып кетейік?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderCity.from_loc)
async def process_city_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderCity.to_loc)
    await message.answer("🏁 <b>Қайда барамыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderCity.to_loc)
async def process_city_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    await state.set_state(OrderCity.price)
    await message.answer("💰 <b>Жол ақысын қанша ұсынасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderCity.price)
async def process_city_price(message: types.Message, state: FSMContext):
    price = message.text
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Көрсетілмеген"

    cursor.execute("INSERT INTO orders (user_id, order_type, from_loc, to_loc, price, phone, status) VALUES (?, 'city', ?, ?, ?, ?, 'new')",
                   (message.from_user.id, data['from_loc'], data['to_loc'], price, phone))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Тапсырысыңыз қабылданды!</b> Жүргізуші іздестірілуде...", reply_markup=main_menu(), parse_mode="HTML")

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛА ІШІНДЕ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {data['from_loc']}\n"
        f"🏁 <b>Қайда:</b> {data['to_loc']}\n"
        f"💰 <b>Жол ақысы:</b> {price}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]])
    await bot.send_message(chat_id=CITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# --- 2. ЗАКАЗ МЕЖГОРОД ---
@dp.message(F.text == "🛣 Қалааралық (Межгород)")
async def start_intercity_order(message: types.Message, state: FSMContext):
    if not await is_client_registered(message.from_user.id):
        await state.set_state(ClientRegister.full_name)
        await message.answer("👤 <b>Тапсырыс беру үшін алдымен тіркелу қажет!</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):", reply_markup=cancel_menu(), parse_mode="HTML")
        return
    await state.set_state(OrderIntercity.from_loc)
    await message.answer("📍 <b>Қай қаладан / ауылдан шығасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderIntercity.from_loc)
async def process_inter_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderIntercity.to_loc)
    await message.answer("🏁 <b>Қай қалаға / ауылға барасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderIntercity.to_loc)
async def process_inter_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    await state.set_state(OrderIntercity.date_time)
    await message.answer("📅 <b>Қай күні және сағат қаншада?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderIntercity.date_time)
async def process_inter_time(message: types.Message, state: FSMContext):
    await state.update_data(date_time=message.text)
    await state.set_state(OrderIntercity.seats)
    await message.answer("👥 <b>Қанша орын керек?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderIntercity.seats)
async def process_inter_seats(message: types.Message, state: FSMContext):
    await state.update_data(seats=message.text)
    await state.set_state(OrderIntercity.price)
    await message.answer("💰 <b>Жол ақысын қанша ұсынасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderIntercity.price)
async def process_inter_price(message: types.Message, state: FSMContext):
    price = message.text
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Көрсетілмеген"

    cursor.execute("INSERT INTO orders (user_id, order_type, from_loc, to_loc, date_time, seats, price, phone, status) VALUES (?, 'intercity', ?, ?, ?, ?, ?, ?, 'new')",
                   (message.from_user.id, data['from_loc'], data['to_loc'], data['date_time'], data['seats'], price, phone))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Қалааралық тапсырыс қабылданды!</b>", reply_markup=main_menu(), parse_mode="HTML")

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛААРАЛЫҚ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {data['from_loc']}\n"
        f"🏁 <b>Қайда:</b> {data['to_loc']}\n"
        f"📅 <b>Уақыты:</b> {data['date_time']}\n"
        f"👥 <b>Орын:</b> {data['seats']}\n"
        f"💰 <b>Жол ақысы:</b> {price}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]])
    await bot.send_message(chat_id=INTERCITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# --- 3. ЗАКАЗ ДОСТАВКИ (ГОРОД + МЕЖГОРОД) ---
@dp.message(F.text == "📦 Жеткізу (Доставка)")
async def start_delivery_order(message: types.Message, state: FSMContext):
    if not await is_client_registered(message.from_user.id):
        await state.set_state(ClientRegister.full_name)
        await message.answer("👤 <b>Тапсырыс беру үшін алдымен тіркелу қажет!</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):", reply_markup=cancel_menu(), parse_mode="HTML")
        return

    await state.set_state(OrderDelivery.delivery_type)
    await message.answer("📦 <b>Жеткізу түрін таңдаңыз:</b>", reply_markup=delivery_menu(), parse_mode="HTML")

@dp.message(OrderDelivery.delivery_type, F.text.in_({"🏙 Қала ішінде", "🛣 Қалааралық (Межгород)"}))
async def process_delivery_type(message: types.Message, state: FSMContext):
    dtype = "city" if message.text == "🏙 Қала ішінде" else "intercity"
    await state.update_data(delivery_type=dtype)
    await state.set_state(OrderDelivery.item_info)
    await message.answer("📦 <b>Не жеткізу керек?</b>\n(Мысалы: Документы, Коробка 5кг, Ключи и т.д.):", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderDelivery.item_info)
async def process_delivery_item(message: types.Message, state: FSMContext):
    await state.update_data(item_info=message.text)
    await state.set_state(OrderDelivery.from_loc)
    await message.answer("📍 <b>Затты қай жерден алып кету керек?</b> (Адрес / объект):", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderDelivery.from_loc)
async def process_delivery_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderDelivery.to_loc)
    await message.answer("🏁 <b>Қай жерге жеткізіп беру керек?</b> (Адрес / объект):", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderDelivery.to_loc)
async def process_delivery_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    await state.set_state(OrderDelivery.price)
    await message.answer("💰 <b>Жеткізу ақысын қанша ұсынасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderDelivery.price)
async def process_delivery_price(message: types.Message, state: FSMContext):
    price = message.text
    data = await state.get_data()
    dtype = data.get('delivery_type', 'city')
    item_info = data.get('item_info', 'Зат / Посылка')
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Көрсетілмеген"

    cursor.execute("INSERT INTO orders (user_id, order_type, delivery_type, item_info, from_loc, to_loc, price, phone, status) VALUES (?, 'delivery', ?, ?, ?, ?, ?, ?, 'new')",
                   (message.from_user.id, dtype, item_info, data['from_loc'], data['to_loc'], price, phone))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Жеткізу тапсырысы қабылданды!</b> Жүргізуші іздестірілуде...", reply_markup=main_menu(), parse_mode="HTML")

    if dtype == "city":
        type_title = "ҚАЛА ІШІНДЕ"
        target_group_id = CITY_GROUP_ID
    else:
        type_title = "ҚАЛААРАЛЫҚ (МЕЖГОРОД)"
        target_group_id = INTERCITY_GROUP_ID

    card_text = (
        f"📦 <b>ЖЕТКІЗУ (ДОСТАВКА - {type_title}) №{order_id}</b>\n\n"
        f"📦 <b>Зат / Посылка:</b> {item_info}\n"
        f"📍 <b>Қайдан:</b> {data['from_loc']}\n"
        f"🏁 <b>Қайда:</b> {data['to_loc']}\n"
        f"💰 <b>Жеткізу ақысы:</b> {price}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]])
    await bot.send_message(chat_id=target_group_id, text=card_text, reply_markup=kb, parse_mode="HTML")

# --- ПРИНЯТИЕ ЗАКАЗА ВОДИТЕЛЕМ ---
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback_query: types.CallbackQuery):
    order_id = callback_query.data.split('_')[1]
    driver = callback_query.from_user
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, order_type, delivery_type, item_info, from_loc, to_loc, date_time, seats, price, phone, status FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()

    if not order or order[10] != 'new':
        await callback_query.answer("Тапсырыс бұрын алынған!", show_alert=True)
        conn.close()
        return

    cursor.execute("SELECT full_name, phone, car_info FROM drivers WHERE user_id = ?", (driver.id,))
    driver_db = cursor.fetchone()

    client_id = order[0]
    cursor.execute("SELECT full_name, phone FROM clients WHERE user_id = ?", (client_id,))
    client_db = cursor.fetchone()

    cursor.execute("UPDATE orders SET status = 'accepted' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    order_type, delivery_type, item_info, from_loc, to_loc, date_time, seats, price, raw_client_phone = order[1], order[2], order[3], order[4], order[5], order[6], order[7], order[8], order[9]

    clean_client_phone = clean_phone_number(raw_client_phone)
    client_name = client_db[0] if client_db else "Клиент"

    driver_name = driver_db[0] if driver_db else driver.full_name
    driver_phone = driver_db[1] if driver_db else "Көрсетілмеген"
    clean_driver_phone = clean_phone_number(driver_phone)
    driver_car = driver_db[2] if driver_db else "Көрсетілмеген"

    # 1. ОБНОВЛЕНИЕ КАРТОЧКИ В ГРУППЕ
    order_label = "📦 ЖЕТКІЗУ (ДОСТАВКА)" if order_type == 'delivery' else "🚖 ТАПСЫРЫС"
    group_card_text = (
        f"✅ <b>{order_label} №{order_id} АЛЫНДЫ!</b>\n\n"
        f"📍 <b>Маршрут:</b> {from_loc} ➔ {to_loc}\n"
        f"💰 <b>Ақысы:</b> {price}\n"
        f"👤 <b>Жүргізуші:</b> {driver_name}"
    )
    try:
        await callback_query.message.edit_text(group_card_text, reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обновления группы: {e}")

    await callback_query.answer("Тапсырысты алдыңыз!")

    # 2. КАРТОЧКА ВОДИТЕЛЮ В ЛС
    if order_type == 'delivery':
        driver_pm_text = (
            f"📦 <b>СІЗ ҚАБЫЛДАҒАН ЖЕТКІЗУ №{order_id}</b>\n\n"
            f"👤 <b>Клиент:</b> {client_name}\n"
            f"📦 <b>Зат / Посылка:</b> {item_info}\n"
            f"📍 <b>Қайдан алып кету:</b> {from_loc}\n"
            f"🏁 <b>Қайда жеткізу:</b> {to_loc}\n"
            f"💰 <b>Жеткізу ақысы:</b> {price}\n"
            f"📞 <b>Телефоны:</b> <a href=\"tel:+{clean_client_phone}\">+{clean_client_phone}</a>"
        )
    else:
        driver_pm_text = (
            f"🚨 <b>СІЗ ҚАБЫЛДАҒАН ТАПСЫРЫС №{order_id}</b>\n\n"
            f"👤 <b>Клиент:</b> {client_name}\n"
            f"📍 <b>Қайдан:</b> {from_loc}\n"
            f"🏁 <b>Қайда:</b> {to_loc}\n"
        )
        if order_type == 'intercity':
            driver_pm_text += f"📅 <b>Уақыты:</b> {date_time}\n👥 <b>Орын:</b> {seats}\n"
        
        driver_pm_text += (
            f"💰 <b>Жол ақысы:</b> {price}\n"
            f"📞 <b>Телефоны:</b> <a href=\"tel:+{clean_client_phone}\">+{clean_client_phone}</a>"
        )

    wa_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 WhatsApp-пен жазу", url=f"https://wa.me/{clean_client_phone}")]
    ])

    try:
        await bot.send_message(chat_id=driver.id, text=driver_pm_text, reply_markup=wa_kb, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки водителю: {e}")

    # 3. КАРТОЧКА КЛИЕНТУ В ЛС
    client_msg = (
        f"🚖 <b>№{order_id} тапсырысыңызды жүргізуші қабылдады!</b>\n\n"
        f"👤 <b>Жүргізуші:</b> {driver_name}\n"
        f"📞 <b>Телефоны:</b> <a href=\"tel:+{clean_driver_phone}\">+{clean_driver_phone}</a>\n"
        f"🚘 <b>Көлігі:</b> {driver_car}\n\n"
        f"Жүргізуші сізбен жақында хабарласады."
    )
    try:
        await bot.send_message(chat_id=client_id, text=client_msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки клиенту: {e}")

# --- ВЕБ-СЕРВЕР ---
async def handle_ping(request):
    return web.Response(text="Bot is live!")

async def main():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # Сброс вебхуков и зависших обновлений перед стартом
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
