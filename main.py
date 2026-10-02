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
    
    # Заказы
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
    
    # Водители
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS drivers (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            car_info TEXT
        )
    ''')

    # Клиенты (Новая таблица)
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

# --- FSM ---
class ClientRegister(StatesGroup):
    full_name = State()
    phone = State()
    next_action = State() # 'city' или 'intercity'

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

class DriverRegister(StatesGroup):
    full_name = State()
    phone = State()
    car_info = State()

# --- МЕНЮ ---
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙 Қала ішінде"), KeyboardButton(text="🛣 Қалааралық (Межгород)")],
            [KeyboardButton(text="🚖 Жүргізуші болу"), KeyboardButton(text="📞 Қолдау қызметі")]
        ],
        resize_keyboard=True
    )

def cancel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Бас тарту")]],
        resize_keyboard=True
    )

# --- СТАРТ И ОТМЕНА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        f"Ассалаумағалейкум, <b>{message.from_user.first_name}</b>!\n\n"
        f"🚖 <b>«Жолдас такси»</b> ботына қош келдіңіз!\n\n"
        f"Керекті бөлімді таңдаңыз 👇"
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")

@dp.message(F.text == "❌ Бас тарту")
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Тоқтатылды.", reply_markup=main_menu())

# --- ВСПУТАТЬ РЕГИСТРАЦИЮ КЛИЕНТА ПЕРЕД ЗАКАЗОМ ---
async def check_or_register_client(message: types.Message, state: FSMContext, action_type: str):
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    conn.close()

    if not client:
        await state.set_state(ClientRegister.full_name)
        await state.update_data(next_action=action_type)
        await message.answer(
            "👤 <b>Тапсырыс беру үшін алдымен тіркелу қажет!</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):",
            reply_markup=cancel_menu(),
            parse_mode="HTML"
        )
        return False
    return True

# --- РЕГИСТРАЦИЯ КЛИЕНТА (ШАГИ) ---
@dp.message(ClientRegister.full_name)
async def process_client_name(message: types.Message, state: FSMContext):
    await state.update_data(client_full_name=message.text)
    await state.set_state(ClientRegister.phone)
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Нөмірді жіберу", request_contact=True)], [KeyboardButton(text="❌ Бас тарту")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("📱 Байланыс телефоныңызды жіберіңіз:", reply_markup=phone_kb)

@dp.message(ClientRegister.phone)
async def process_client_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO clients (user_id, full_name, phone) VALUES (?, ?, ?)",
                   (message.from_user.id, data['client_full_name'], phone))
    conn.commit()
    conn.close()

    next_action = data.get('next_action')
    await message.answer("✅ <b>Сіз сәтті тіркелдіңіз!</b>", parse_mode="HTML")

    if next_action == "city":
        await state.set_state(OrderCity.from_loc)
        await message.answer("📍 <b>Қайдан алып кетейік?</b>", reply_markup=cancel_menu(), parse_mode="HTML")
    elif next_action == "intercity":
        await state.set_state(OrderIntercity.from_loc)
        await message.answer("📍 <b>Қай қаладан / ауылдан шығасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")
    else:
        await state.clear()

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
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Нөмірді жіберу", request_contact=True)], [KeyboardButton(text="❌ Бас тарту")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("📱 Байланыс телефоныңызды жіберіңіз:", reply_markup=phone_kb)

@dp.message(DriverRegister.phone)
async def process_driver_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
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
        
        await message.answer(
            "🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b>\n\n"
            "Енді тапсырыстарды көру үшін төмендегі батырмалар арқылы жұмыс топтарына қосылыңыз 👇",
            reply_markup=group_kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не удалось сгенерировать ссылки: {e}")
        await message.answer("🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b> Енді жұмыс топтарына қосылу өтінішін жіберіңіз!", reply_markup=main_menu(), parse_mode="HTML")

# --- АВТО-ОДОБРЕНИЕ ЗАЯВОК ---
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
    is_registered = await check_or_register_client(message, state, "city")
    if not is_registered:
        return
    
    await state.set_state(OrderCity.from_loc)
    await message.answer("📍 <b>Қайдан алып кетейік?</b>", reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(OrderCity.from_loc)
async def process_city_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderCity.to_loc)
    await message.answer("🏁 <b>Қайда барамыз?</b>", parse_mode="HTML")

@dp.message(OrderCity.to_loc)
async def process_city_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    await state.set_state(OrderCity.price)
    await message.answer("💰 <b>Жолқыны қанша ұсынасыз?</b>", parse_mode="HTML")

@dp.message(OrderCity.price)
async def process_city_price(message: types.Message, state: FSMContext):
    price = message.text
    data = await state.get_data()
    
    # Берем телефон из базы зарегистрированных клиентов
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Незвестно"

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
        f"💰 <b>Бағасы:</b> {price}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]])
    await bot.send_message(chat_id=CITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# --- 2. ЗАКАЗ МЕЖГОРОД ---
@dp.message(F.text == "🛣 Қалааралық (Межгород)")
async def start_intercity_order(message: types.Message, state: FSMContext):
    is_registered = await check_or_register_client(message, state, "intercity")
    if not is_registered:
        return

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
    await message.answer("📅 <b>Қай күні және сағат қаншада?</b>", parse_mode="HTML")

@dp.message(OrderIntercity.date_time)
async def process_inter_time(message: types.Message, state: FSMContext):
    await state.update_data(date_time=message.text)
    await state.set_state(OrderIntercity.seats)
    await message.answer("👥 <b>Қанша орын керек?</b>", parse_mode="HTML")

@dp.message(OrderIntercity.seats)
async def process_inter_seats(message: types.Message, state: FSMContext):
    await state.update_data(seats=message.text)
    await state.set_state(OrderIntercity.price)
    await message.answer("💰 <b>Ұсынатын бағаңыз:</b>", parse_mode="HTML")

@dp.message(OrderIntercity.price)
async def process_inter_price(message: types.Message, state: FSMContext):
    price = message.text
    data = await state.get_data()
    
    # Берем телефон из базы зарегистрированных клиентов
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Неизвестно"

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
        f"💰 <b>Бағасы:</b> {price}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]])
    await bot.send_message(chat_id=INTERCITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# --- ПРИНЯТИЕ ЗАКАЗА ВОДИТЕЛЕМ ---
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback_query: types.CallbackQuery):
    order_id = callback_query.data.split('_')[1]
    driver = callback_query.from_user
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, order_type, from_loc, to_loc, price, phone, status FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()

    if not order or order[6] != 'new':
        await callback_query.answer("Тапсырыс бұрын алынған!", show_alert=True)
        conn.close()
        return

    # Данные водителя из базы
    cursor.execute("SELECT full_name, phone, car_info FROM drivers WHERE user_id = ?", (driver.id,))
    driver_db = cursor.fetchone()

    # Фиксируем принятие
    cursor.execute("UPDATE orders SET status = 'accepted' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    client_id, _, from_loc, to_loc, price, raw_client_phone = order[0], order[1], order[2], order[3], order[4], order[5]

    # Форматируем телефон клиента
    client_digits = ''.join(filter(str.isdigit, str(raw_client_phone)))
    if len(client_digits) == 10:
        clean_client_phone = "7" + client_digits
    elif len(client_digits) == 11 and client_digits.startswith("8"):
        clean_client_phone = "7" + client_digits[1:]
    else:
        clean_client_phone = client_digits

    # Формируем данные водителя
    driver_name = driver_db[0] if driver_db else driver.full_name
    driver_phone = driver_db[1] if driver_db else "Көрсетілмеген"
    driver_car = driver_db[2] if driver_db else "Көрсетілмеген"

    # 1. Обновляем карточку прямо в группе для водителя
    group_card_text = (
        f"✅ <b>ТАПСЫРЫС №{order_id} АЛЫНДЫ!</b>\n\n"
        f"📍 <b>Маршрут:</b> {from_loc} ➔ {to_loc}\n"
        f"💰 <b>Бағасы:</b> {price}\n"
        f"👤 <b>Жүргізуші:</b> {driver_name}\n"
        f"----------------------------\n"
        f"📱 <b>Клиент нөмірі:</b> <code>+{clean_client_phone}</code>"
    )
    wa_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 WhatsApp-пен жазу", url=f"https://wa.me/{clean_client_phone}")]
    ])

    try:
        await callback_query.message.edit_text(group_card_text, reply_markup=wa_kb, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обновления группы: {e}")

    await callback_query.answer("Тапсырысты алдыңыз!")

    # 2. Отправляем полноценную карточку водителя клиенту в ЛС
    client_msg = (
        f"🚖 <b>№{order_id} тапсырысыңызды жүргізуші қабылдады!</b>\n\n"
        f"👤 <b>Жүргізуші:</b> {driver_name}\n"
        f"📞 <b>Телефоны:</b> <code>{driver_phone}</code>\n"
        f"🚘 <b>Көлігі:</b> {driver_car}\n\n"
        f"Жүргізуші сізбен жақында хабарласады."
    )
    try:
        await bot.send_message(chat_id=client_id, text=client_msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки клиенту: {e}")

# --- ВЕБ-СЕРВЕР ДЛЯ РЕНДЕРА И ЗАПУСК ---
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

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
