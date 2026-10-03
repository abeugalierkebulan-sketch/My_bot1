import logging
import sqlite3
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)

# --- НАСТРОЙКИ (Замените на свои данные) ---
BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"
GROUP_ID = -1001234567890  # ID вашей Telegram группы (начинается с -100)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    
    # Таблица клиентов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT
        )
    """)
    
    # Таблица водителей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            car_info TEXT
        )
    """)
    
    # Таблица заказов
    cursor.execute("""
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
            status TEXT DEFAULT 'new'
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_phone_number(phone: str) -> str:
    """Очищает номер телефона, оставляя только цифры без плюса (для tel: и wa.me)"""
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('8') and len(digits) == 11:
        digits = '7' + digits[1:]
    return digits

# --- СОСТОЯНИЯ (FSM) ---
class ClientRegisterGroup(StatesGroup):
    name = State()
    phone = State()

class DriverRegisterGroup(StatesGroup):
    name = State()
    phone = State()
    car = State()

class OrderGroup(StatesGroup):
    order_type = State()
    delivery_type = State()
    item_info = State()
    from_loc = State()
    to_loc = State()
    date_time = State()
    seats = State()
    price = State()
    phone = State()

# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚕 Тапсырыс беру (Заказать)")],
            [KeyboardButton(text="🚖 Жүргізуші болып тіркелу"), KeyboardButton(text="👤 Клиент болып тіркелу")]
        ],
        resize_keyboard=True
    )

def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Номерді жіберу", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def order_type_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙️ Қала іші (Город)")],
            [KeyboardButton(text="🚘 Қалааралық (Межгород)")],
            [KeyboardButton(text="📦 Жеткізу (Доставка)")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def delivery_type_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📄 Құжаттар / Заттар")],
            [KeyboardButton(text="📦 Ауыр / Үлкен жүк")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# --- СТАРТ И РЕГИСТРАЦИЯ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Сәлеметсіз бе! Жолдас Такси ботына кош келдіңіз!\n"
        "Төмендегі мәзірден керекті бөлімді таңдаңыз:",
        reply_markup=main_menu_kb()
    )

# --- РЕГИСТРАЦИЯ КЛИЕНТА ---
@dp.message(F.text == "👤 Клиент болып тіркелу")
async def start_client_reg(message: types.Message, state: FSMContext):
    await state.set_state(ClientRegisterGroup.name)
    await message.answer("Аты-жөніңізді енгізіңіз:", reply_markup=ReplyKeyboardRemove())

@dp.message(ClientRegisterGroup.name)
async def process_client_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(ClientRegisterGroup.phone)
    await message.answer("Телефон номеріңізді төмендегі батырма арқылы жіберіңіз немесе жазыңыз:", reply_markup=phone_kb())

@dp.message(ClientRegisterGroup.phone)
async def process_client_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO clients (user_id, full_name, phone) VALUES (?, ?, ?)",
                   (message.from_user.id, data['name'], phone))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("✅ Тіркелу сәтті аяқталды!", reply_markup=main_menu_kb())

# --- РЕГИСТРАЦИЯ ВОДИТЕЛЯ ---
@dp.message(F.text == "🚖 Жүргізуші болып тіркелу")
async def start_driver_reg(message: types.Message, state: FSMContext):
    await state.set_state(DriverRegisterGroup.name)
    await message.answer("Аты-жөніңізді енгізіңіз:", reply_markup=ReplyKeyboardRemove())

@dp.message(DriverRegisterGroup.name)
async def process_driver_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(DriverRegisterGroup.phone)
    await message.answer("Телефон номеріңізді жіберіңіз:", reply_markup=phone_kb())

@dp.message(DriverRegisterGroup.phone)
async def process_driver_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    await state.set_state(DriverRegisterGroup.car)
    await message.answer("Көлігіңіздің маркасы мен номерін жазыңыз (мысалы: Toyota Camry 001 AAA 05):", reply_markup=ReplyKeyboardRemove())

@dp.message(DriverRegisterGroup.car)
async def process_driver_car(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO drivers (user_id, full_name, phone, car_info) VALUES (?, ?, ?, ?)",
                   (message.from_user.id, data['name'], data['phone'], message.text))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("✅ Жүргізуші болып сәтті тіркелдіңіз!", reply_markup=main_menu_kb())

# --- СОЗДАНИЕ ЗАКАЗА ---
@dp.message(F.text == "🚕 Тапсырыс беру (Заказать)")
async def start_order(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    client = cursor.fetchone()
    conn.close()

    if not client:
        await message.answer("⚠️ Тапсырыс беру үшін алдымен «👤 Клиент болып тіркелу» батырмасын басыңыз!")
        return

    await state.set_state(OrderGroup.order_type)
    await message.answer("Тапсырыс түрін таңдаңыз:", reply_markup=order_type_kb())

@dp.message(OrderGroup.order_type)
async def process_order_type(message: types.Message, state: FSMContext):
    txt = message.text
    if "Қала іші" in txt:
        await state.update_data(order_type='city')
        await state.set_state(OrderGroup.from_loc)
        await message.answer("Қайдан алып кету керек? (Адрес):", reply_markup=ReplyKeyboardRemove())
    elif "Қалааралық" in txt:
        await state.update_data(order_type='intercity')
        await state.set_state(OrderGroup.from_loc)
        await message.answer("Қай ауылдан / қаладан?:", reply_markup=ReplyKeyboardRemove())
    elif "Жеткізу" in txt:
        await state.update_data(order_type='delivery')
        await state.set_state(OrderGroup.delivery_type)
        await message.answer("Жеткізу түрін таңдаңыз:", reply_markup=delivery_type_kb())

@dp.message(OrderGroup.delivery_type)
async def process_delivery_type(message: types.Message, state: FSMContext):
    await state.update_data(delivery_type=message.text)
    await state.set_state(OrderGroup.item_info)
    await message.answer("Не жеткізу керек? (Зат / Посылка туралы қысқаша):")

@dp.message(OrderGroup.item_info)
async def process_item_info(message: types.Message, state: FSMContext):
    await state.update_data(item_info=message.text)
    await state.set_state(OrderGroup.from_loc)
    await message.answer("Қайдан алып кету керек? (Адрес):")

@dp.message(OrderGroup.from_loc)
async def process_from_loc(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=message.text)
    await state.set_state(OrderGroup.to_loc)
    await message.answer("Қайда жеткізу керек? (Баратын адрес):")

@dp.message(OrderGroup.to_loc)
async def process_to_loc(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=message.text)
    data = await state.get_data()

    if data['order_type'] == 'intercity':
        await state.set_state(OrderGroup.date_time)
        await message.answer("Қай күні және қай уақытта? (мысалы: Бүгін сағат 15:00-де):")
    else:
        await state.set_state(OrderGroup.price)
        await message.answer("Ұсынатын жол ақыңыз (суммасы):")

@dp.message(OrderGroup.date_time)
async def process_date_time(message: types.Message, state: FSMContext):
    await state.update_data(date_time=message.text)
    await state.set_state(OrderGroup.seats)
    await message.answer("Қанша адам / орын?:")

@dp.message(OrderGroup.seats)
async def process_seats(message: types.Message, state: FSMContext):
    await state.update_data(seats=message.text)
    await state.set_state(OrderGroup.price)
    await message.answer("Ұсынатын жол ақыңыз (суммасы):")

@dp.message(OrderGroup.price)
async def process_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = ?", (message.from_user.id,))
    phone = cursor.fetchone()[0]
    conn.close()

    await state.update_data(phone=phone)
    data = await state.get_data()

    # Сохраняем заказ в БД
    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO orders (user_id, order_type, delivery_type, item_info, from_loc, to_loc, date_time, seats, price, phone, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
    """, (
        message.from_user.id,
        data.get('order_type'),
        data.get('delivery_type', ''),
        data.get('item_info', ''),
        data.get('from_loc'),
        data.get('to_loc'),
        data.get('date_time', ''),
        data.get('seats', ''),
        data.get('price'),
        phone
    ))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Оформляем текст карточки в группу
    if data['order_type'] == 'delivery':
        card_text = (
            f"📦 <b>ЖАҢА ЖЕТКІЗУ №{order_id}</b>\n\n"
            f"📄 <b>Түрі:</b> {data.get('delivery_type')}\n"
            f"📦 <b>Зат:</b> {data.get('item_info')}\n"
            f"📍 <b>Қайдан:</b> {data.get('from_loc')}\n"
            f"🏁 <b>Қайда:</b> {data.get('to_loc')}\n"
            f"💰 <b>Ақысы:</b> {data.get('price')}\n"
        )
    else:
        type_str = "🏙️️ ҚАЛА ІШІ" if data['order_type'] == 'city' else "🚘 ҚАЛААРАЛЫҚ"
        card_text = (
            f"🚖 <b>ЖАҢА ТАПСЫРЫС №{order_id} ({type_str})</b>\n\n"
            f"📍 <b>Қайдан:</b> {data.get('from_loc')}\n"
            f"🏁 <b>Қайда:</b> {data.get('to_loc')}\n"
        )
        if data['order_type'] == 'intercity':
            card_text += f"📅 <b>Уақыты:</b> {data.get('date_time')}\n👥 <b>Орын:</b> {data.get('seats')}\n"
        
        card_text += f"💰 <b>Жол ақысы:</b> {data.get('price')}\n"

    accept_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]
    ])

    # Отправляем карточку в группу
    try:
        await bot.send_message(chat_id=GROUP_ID, text=card_text, reply_markup=accept_kb, parse_mode="HTML")
        await message.answer("✅ Тапсырысыңыз топқа жіберілді! Жүргізуші қабылдағанда сізге хабарлама келеді.", reply_markup=main_menu_kb())
    except Exception as e:
        logging.error(f"Ошибка отправки в группу: {e}")
        await message.answer("⚠️ Қате: Топқа хабарлама жіберілмеді. Бот топқа қосылғанын тексеріңіз.", reply_markup=main_menu_kb())

    await state.clear()


# --- ПРИНЯТИЕ ЗАКАЗА ВОДИТЕЛЕМ ---
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback_query: types.CallbackQuery):
    order_id = callback_query.data.split('_')[1]
    driver = callback_query.from_user

    conn = sqlite3.connect("joldas_taxi.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, order_type, delivery_type, item_info, from_loc, to_loc, date_time, seats, price, phone, status FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()

    # Проверка: если заказ уже кто-то взял
    if not order or order[10] != 'new':
        await callback_query.answer("⚠️ Бұл тапсырысты басқа жүргізуші алып қойған!", show_alert=True)
        conn.close()
        return

    # Получаем данные водителя и клиента
    cursor.execute("SELECT full_name, phone, car_info FROM drivers WHERE user_id = ?", (driver.id,))
    driver_db = cursor.fetchone()

    client_id = order[0]
    cursor.execute("SELECT full_name, phone FROM clients WHERE user_id = ?", (client_id,))
    client_db = cursor.fetchone()

    # Обновляем статус заказа
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

    # 1. Быстрое всплывающее уведомление сверху экрана
    await callback_query.answer("✅ Тапсырыс қабылданды! Ботты ашыңыз.")

    # 2. ОБНОВЛЕНИЕ КАРТОЧКИ В ГРУППЕ (Удаляем все кнопки)
    order_label = "📦 ЖЕТКІЗУ (ДОСТАВКА)" if order_type == 'delivery' else "🚖 ТАПСЫРЫС"
    group_card_text = (
        f"✅ <b>{order_label} №{order_id} АЛЫНДЫ!</b>\n\n"
        f"📍 <b>Маршрут:</b> {from_loc} ➔ {to_loc}\n"
        f"💰 <b>Ақысы:</b> {price}\n"
        f"👤 <b>Жүргізуші:</b> {driver_name}"
    )

    try:
        # reply_markup=None полностью убирает кнопки из группы
        await callback_query.message.edit_text(group_card_text, reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обновления группы: {e}")

    # 3. КАРТОЧКА ВОДИТЕЛЮ В ЛС (Приходит как НЕПРОЧИТАННОЕ со звуковым сигналом)
    if order_type == 'delivery':
        driver_pm_text = (
            f"🔔 <b>СІЗ ҚАБЫЛДАҒАН ЖЕТКІЗУ №{order_id}</b>\n\n"
            f"👤 <b>Клиент:</b> {client_name}\n"
            f"📦 <b>Зат / Посылка:</b> {item_info}\n"
            f"📍 <b>Қайдан алып кету:</b> {from_loc}\n"
            f"🏁 <b>Қайда жеткізу:</b> {to_loc}\n"
            f"💰 <b>Жеткізу ақысы:</b> {price}\n"
            f"📞 <b>Телефоны:</b> <a href=\"tel:+{clean_client_phone}\">+{clean_client_phone}</a>"
        )
    else:
        driver_pm_text = (
            f"🔔 <b>СІЗ ҚАБЫЛДАҒАН ТАПСЫРЫС №{order_id}</b>\n\n"
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
        await bot.send_message(
            chat_id=driver.id,
            text=driver_pm_text,
            reply_markup=wa_kb,
            parse_mode="HTML",
            disable_notification=False
        )
    except Exception as e:
        logging.error(f"Ошибка отправки водителю: {e}")

    # 4. УВЕДОМЛЕНИЕ КЛИЕНТУ В ЛС
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


# --- ЗАПУСК БОТА ---
async def main():
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
