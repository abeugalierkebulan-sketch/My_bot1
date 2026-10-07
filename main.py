import os
import logging
import asyncio
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ChatJoinRequest
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# Настройки из Environment Variables (Render)
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").replace("@", "")

CITY_GROUP_ID = int(os.getenv("CITY_GROUP_ID", "-1004350443552"))
INTERCITY_GROUP_ID = int(os.getenv("INTERCITY_GROUP_ID", "-1003756709241"))

COMMISSION_PERCENT = 10 # Процент комиссии от заказа (10%)

DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ (PostgreSQL) ---
def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL переменная окружения не найдена!")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Таблица заказов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
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
    
    # Таблица водителей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS drivers (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT,
            phone TEXT UNIQUE,
            car_info TEXT,
            balance REAL DEFAULT 0.0,
            has_received_bonus BOOLEAN DEFAULT FALSE
        )
    ''')

    # Таблица клиентов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT,
            phone TEXT UNIQUE
        )
    ''')

    conn.commit()
    cursor.close()
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
    delivery_type = State()
    item_info = State()
    from_loc = State()
    to_loc = State()
    price = State()

class DriverRegister(StatesGroup):
    full_name = State()
    phone = State()
    car_info = State()

class TopupState(StatesGroup):
    waiting_for_receipt = State()
    waiting_custom_amount = State()

# --- ВАЛИДАЦИЯ И ОЧИСТКА НОМЕРА ---
def validate_phone(raw_phone: str) -> str | None:
    digits = ''.join(filter(str.isdigit, str(raw_phone)))
    if len(digits) == 11 and digits.startswith(('7', '8')):
        return "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith('7'):
        return "7" + digits
    return None

def clean_phone_number(raw_phone: str) -> str:
    phone = validate_phone(raw_phone)
    return phone if phone else ''.join(filter(str.isdigit, str(raw_phone)))

def parse_price(price_str: str) -> float:
    digits = re.findall(r'\d+', str(price_str))
    if digits:
        return float(''.join(digits))
    return 0.0

# --- МЕНЮ ---
def main_menu(user_id: int = None):
    keyboard = [
        [KeyboardButton(text="🏙 Қала ішінде"), KeyboardButton(text="🛣 Қалааралық (Межгород)")],
        [KeyboardButton(text="📦 Жеткізу (Доставка)"), KeyboardButton(text="🚖 Жүргізуші болу")],
        [KeyboardButton(text="📞 Қолдау қызметі (Админге жазу)")]
    ]
    
    if user_id and is_driver_registered(user_id):
        keyboard.append([KeyboardButton(text="💰 Менің балансым"), KeyboardButton(text="💳 Баланс толтыру")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def delivery_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙 Қала ішінде жеткізу"), KeyboardButton(text="🛣 Қалааралық жеткізу")],
            [KeyboardButton(text="❌ Бас тарту")]
        ],
        resize_keyboard=True
    )

def cancel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Бас тарту")]],
        resize_keyboard=True
    )

def phone_request_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Номерді жіберу", request_contact=True)],
            [KeyboardButton(text="❌ Бас тарту")]
        ],
        resize_keyboard=True
    )

def is_driver_registered(user_id: int) -> bool:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM drivers WHERE user_id = %s", (user_id,))
        driver = cursor.fetchone()
        cursor.close()
        conn.close()
        return driver is not None
    except Exception as e:
        logging.error(f"Ошибка проверки водителя: {e}")
        return False

async def is_client_registered(user_id: int) -> bool:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT full_name FROM clients WHERE user_id = %s", (user_id,))
        client = cursor.fetchone()
        cursor.close()
        conn.close()
        return client is not None
    except Exception as e:
        logging.error(f"Ошибка проверки клиента: {e}")
        return False

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
        await message.answer(text, reply_markup=main_menu(message.from_user.id), parse_mode="HTML")
    else:
        await state.set_state(ClientRegister.full_name)
        text = (
            f"Ассалаумағалейкум, <b>{message.from_user.first_name}</b>!\n\n"
            f"🚖 <b>«Жолдас такси»</b> қызметін пайдалану үшін алдымен тіркелу қажет.\n\n"
            f"👤 <b>Аты-жөніңізді жазыңыз (ФИО):</b>"
        )
        await message.answer(text, reply_markup=cancel_menu(), parse_mode="HTML")

# --- СТАТИСТИКА ДЛЯ АДМИНА ---
@dp.message(Command("stats"))
async def admin_get_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM clients;")
        clients_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM drivers;")
        drivers_count = cursor.fetchone()[0]

        cursor.close()
        conn.close()

        total = clients_count + drivers_count

        stats_text = (
            f"📊 <b>СТАТИСТИКА БОТА «ЖОЛДАС ТАКСИ»:</b>\n\n"
            f"👨‍✈️ <b>Жүргізушілер (Водители):</b> {drivers_count}\n"
            f"👤 <b>Клиенттер (Пассажиры):</b> {clients_count}\n"
            f"➖➖➖➖➖➖➖➖➖\n"
            f"👥 <b>Барлығы (Всего пользователей):</b> {total}"
        )
        await message.answer(stats_text, parse_mode="HTML")

    except Exception as e:
        logging.error(f"Ошибка получения статистики: {e}")
        await message.answer("❌ Ошибка при получении статистики из базы данных.")

# --- ОТМЕНА ---
@dp.message(F.text.in_({"❌ Бас тарту", "⬅️ Қайту (артқа)"}))
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_client_registered(message.from_user.id):
        await message.answer("❌ Тоқтатылды.", reply_markup=main_menu(message.from_user.id))
    else:
        await message.answer("❌ Тоқтатылды. Қайта бастау үшін /start басыңыз.", reply_markup=types.ReplyKeyboardRemove())

# --- КНОПКА ПОДДЕРЖКИ (АДМИН) ---
@dp.message(F.text == "📞 Қолдау қызметі (Админге жазу)")
async def support_contact(message: types.Message):
    url = f"https://t.me/{ADMIN_USERNAME}" if ADMIN_USERNAME else f"tg://user?id={ADMIN_ID}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Админге жазу (ЛС)", url=url)]
    ])
    text = (
        f"📞 <b>Қолдау қызметі</b>\n\n"
        f"Сұрақтарыңыз немесе ұсыныстарыңыз болса, төмендегі батырманы басып админге жаза аласыз 👇"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

# --- БАЛАНС И ПОПОЛНЕНИЕ ВОДИТЕЛЯ ---
@dp.message(F.text == "💰 Менің балансым")
async def show_driver_balance(message: types.Message):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM drivers WHERE user_id = %s", (message.from_user.id,))
    res = cursor.fetchone()
    cursor.close()
    conn.close()

    if res:
        balance = res[0]
        text = (
            f"💰 <b>Сіздің балансыңыз:</b> {balance:.0f} ₸\n\n"
            f"📌 Балансты толтыру үшін «💳 Баланс толтыру» батырмасын басып, Kaspi чегін жіберіңіз."
        )
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer("❌ Сіз жүргізуші ретінде тіркелмегенсіз.")

@dp.message(F.text == "💳 Баланс толтыру")
async def request_topup_receipt(message: types.Message, state: FSMContext):
    if not is_driver_registered(message.from_user.id):
        await message.answer("❌ Сіз жүргізуші ретінде тіркелмегенсіз.")
        return

    await state.set_state(TopupState.waiting_for_receipt)
    text = (
        f"💳 <b>Балансты Kaspi арқылы толтыру:</b>\n\n"
        f"1. Kaspi арқылы мына номерге аударыңыз: <b>+7 775 699 63 09 (Еркебұлан Ә.)</b>\n"
        f"2. Төлем жасап болған соң, <b>чектің суретін (скриншот) немесе файлын дәл осы чатқа жіберіңіз</b> 👇"
    )
    await message.answer(text, reply_markup=cancel_menu(), parse_mode="HTML")

@dp.message(TopupState.waiting_for_receipt, F.photo | F.document)
async def process_topup_receipt(message: types.Message, state: FSMContext):
    driver_id = message.from_user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, phone, has_received_bonus FROM drivers WHERE user_id = %s", (driver_id,))
    driver = cursor.fetchone()
    cursor.close()
    conn.close()

    driver_name = driver[0] if driver else message.from_user.full_name
    driver_phone = driver[1] if driver else "Нет номера"
    has_bonus = driver[2] if driver else False

    bonus_status = "⚠️ УЖЕ ПОЛУЧАЛ 5000 ₸ БОНУС" if has_bonus else "🎁 ЕЩЕ НЕ ПОЛУЧАЛ БОНУС (МОЖНО ДАТЬ 5000 ₸)"

    caption = (
        f"💳 <b>ЖАҢА ТӨЛЕМ ЧЕГІ!</b>\n\n"
        f"👤 <b>Жүргізуші:</b> {driver_name}\n"
        f"📞 <b>Тел:</b> {driver_phone}\n"
        f"🆔 <b>ID:</b> <code>{driver_id}</code>\n"
        f"📌 <b>Статус бонуса:</b> {bonus_status}\n\n"
        f"Толтырылатын сумманы таңдаңыз 👇"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎁 БОНУС 5000 ₸", callback_data=f"paybonus_{driver_id}_5000")
        ],
        [
            InlineKeyboardButton(text="➕ 500 ₸", callback_data=f"pay_{driver_id}_500"),
            InlineKeyboardButton(text="➕ 1000 ₸", callback_data=f"pay_{driver_id}_1000")
        ],
        [
            InlineKeyboardButton(text="➕ 2000 ₸", callback_data=f"pay_{driver_id}_2000"),
            InlineKeyboardButton(text="➕ 5000 ₸", callback_data=f"pay_{driver_id}_5000")
        ],
        [
            InlineKeyboardButton(text="✏️ Басқа сумма", callback_data=f"paycustom_{driver_id}")
        ]
    ])

    try:
        if message.photo:
            await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
        elif message.document:
            await bot.send_document(chat_id=ADMIN_ID, document=message.document.file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
        
        await state.clear()
        await message.answer("✅ <b>Чек администраторға жіберілді!</b> Балансыңыз жақын арада толтырылады.", reply_markup=main_menu(driver_id), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки чека админу: {e}")
        await message.answer("❌ Чекті жіберу кезінде қате шықты. Админге хабарласыңыз.")

# --- ОБРАБОТКА ПОПОЛНЕНИЯ И БОНУСА АДМИНОМ ---
@dp.callback_query(F.data.startswith("paybonus_"))
async def admin_bonus_pay(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return

    parts = callback_query.data.split("_")
    target_id = int(parts[1])
    amount = float(parts[2])

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT balance, has_received_bonus FROM drivers WHERE user_id = %s", (target_id,))
    driver = cursor.fetchone()

    if not driver:
        await callback_query.answer("❌ Жүргізуші табылмады!", show_alert=True)
        cursor.close()
        conn.close()
        return

    old_balance, has_bonus = driver[0], driver[1]

    if has_bonus:
        await callback_query.answer("⚠️ Бұл жүргізуші 5000 ₸ бонусты бұрын алып қойған!", show_alert=True)
        cursor.close()
        conn.close()
        return

    new_balance = old_balance + amount
    cursor.execute("UPDATE drivers SET balance = %s, has_received_bonus = TRUE WHERE user_id = %s", (new_balance, target_id))
    conn.commit()
    cursor.close()
    conn.close()

    await callback_query.message.edit_caption(
        caption=callback_query.message.caption + f"\n\n🎁 <b>БОНУС 5000 ₸ БЕРІЛДІ!</b>\nБаланс: {new_balance:.0f} ₸",
        reply_markup=None,
        parse_mode="HTML"
    )

    try:
        notify_text = (
            f"🎉 <b>Сізге 5000 ₸ ашылу бонусы берілді!</b>\n\n"
            f"💰 <b>Бұрынғы баланс:</b> {old_balance:.0f} ₸\n"
            f"🎁 <b>Бонус:</b> {amount:.0f} ₸\n"
            f"💵 <b>Ағымдағы баланс:</b> {new_balance:.0f} ₸"
        )
        await bot.send_message(chat_id=target_id, text=notify_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление: {e}")

@dp.callback_query(F.data.startswith("pay_"))
async def admin_quick_pay(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return

    parts = callback_query.data.split("_")
    target_id = int(parts[1])
    amount = float(parts[2])

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM drivers WHERE user_id = %s", (target_id,))
    driver = cursor.fetchone()

    if not driver:
        await callback_query.answer("❌ Жүргізуші табылмады!", show_alert=True)
        cursor.close()
        conn.close()
        return

    old_balance = driver[0]
    new_balance = old_balance + amount
    cursor.execute("UPDATE drivers SET balance = %s WHERE user_id = %s", (new_balance, target_id))
    conn.commit()
    cursor.close()
    conn.close()

    await callback_query.message.edit_caption(
        caption=callback_query.message.caption + f"\n\n✅ <b>ТОЛТЫРЫЛДЫ: +{amount:.0f} ₸</b>\nБаланс: {new_balance:.0f} ₸",
        reply_markup=None,
        parse_mode="HTML"
    )

    try:
        notify_text = (
            f"🎉 <b>Балансыңыз толтырылды!</b>\n\n"
            f"💰 <b>Бұрынғы баланс:</b> {old_balance:.0f} ₸\n"
            f"➕ <b>Қосылды:</b> {amount:.0f} ₸\n"
            f"💵 <b>Ағымдағы баланс:</b> {new_balance:.0f} ₸"
        )
        await bot.send_message(chat_id=target_id, text=notify_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление: {e}")

@dp.callback_query(F.data.startswith("paycustom_"))
async def admin_custom_pay_start(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.from_user.id != ADMIN_ID:
        return

    target_id = int(callback_query.data.split("_")[1])
    await state.update_data(target_driver_id=target_id)
    await state.set_state(TopupState.waiting_custom_amount)
    await callback_query.message.answer(f"✍️ ID {target_id} жүргізушіге қанша сумма қосқыңыз келеді? (тек сан жазыңыз, мысалы: 1500)")
    await callback_query.answer()

@dp.message(TopupState.waiting_custom_amount)
async def admin_custom_pay_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        amount = float(message.text.strip())
        data = await state.get_data()
        target_id = data.get("target_driver_id")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM drivers WHERE user_id = %s", (target_id,))
        driver = cursor.fetchone()

        if not driver:
            await message.answer("❌ Жүргізуші табылмады!")
            cursor.close()
            conn.close()
            await state.clear()
            return

        old_balance = driver[0]
        new_balance = old_balance + amount
        cursor.execute("UPDATE drivers SET balance = %s WHERE user_id = %s", (new_balance, target_id))
        conn.commit()
        cursor.close()
        conn.close()

        await state.clear()
        await message.answer(f"✅ ID {target_id} жүргізушіге {amount:.0f} ₸ толтырылды!\nБұрын: {old_balance:.0f} ₸ ➔ Қазір: {new_balance:.0f} ₸")

        try:
            notify_text = (
                f"🎉 <b>Балансыңыз толтырылды!</b>\n\n"
                f"💰 <b>Бұрынғы баланс:</b> {old_balance:.0f} ₸\n"
                f"➕ <b>Қосылды:</b> {amount:.0f} ₸\n"
                f"💵 <b>Ағымдағы баланс:</b> {new_balance:.0f} ₸"
            )
            await bot.send_message(chat_id=target_id, text=notify_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка уведомления водителю: {e}")

    except ValueError:
        await message.answer("❌ Тек сан енгізіңіз! Мысалы: 1500")

@dp.message(Command("pay"))
async def admin_topup_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        args = message.text.split()
        target_id = int(args[1])
        amount = float(args[2])

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM drivers WHERE user_id = %s", (target_id,))
        driver = cursor.fetchone()

        if not driver:
            await message.answer("❌ Жүргізуші табылмады!")
            cursor.close()
            conn.close()
            return

        old_balance = driver[0]
        new_balance = old_balance + amount
        cursor.execute("UPDATE drivers SET balance = %s WHERE user_id = %s", (new_balance, target_id))
        conn.commit()
        cursor.close()
        conn.close()

        await message.answer(f"✅ ID {target_id} жүргізушіге {amount:.0f} ₸ толтырылды! Жаңа баланс: {new_balance:.0f} ₸")
        
        try:
            notify_text = (
                f"🎉 <b>Балансыңыз толтырылды!</b>\n\n"
                f"💰 <b>Бұрынғы баланс:</b> {old_balance:.0f} ₸\n"
                f"➕ <b>Қосылды:</b> {amount:.0f} ₸\n"
                f"💵 <b>Ағымдағы баланс:</b> {new_balance:.0f} ₸"
            )
            await bot.send_message(chat_id=target_id, text=notify_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление водителю: {e}")

    except Exception:
        await message.answer("❌ Формат қате! Қолдану: `/pay USER_ID SUMMA`", parse_mode="Markdown")

# --- РЕГИСТРАЦИЯ КЛИЕНТА ---
@dp.message(ClientRegister.full_name)
async def process_client_name(message: types.Message, state: FSMContext):
    await state.update_data(client_full_name=message.text)
    await state.set_state(ClientRegister.phone)
    await message.answer(
        "📱 <b>Байланыс телефоныңызды енгізіңіз:</b>\n\n"
        "Төмендегі «📱 Номерді жіберу» батырмасын басыңыз немесе қолдан жазыңыз (мысалы: 87071234567):",
        reply_markup=phone_request_menu(),
        parse_mode="HTML"
    )

@dp.message(ClientRegister.phone, F.contact | F.text)
async def process_client_phone(message: types.Message, state: FSMContext):
    raw_phone = message.contact.phone_number if message.contact else message.text
    phone = validate_phone(raw_phone)

    # 1. Валидация формата
    if not phone:
        await message.answer("❌ <b>Қате нөмір!</b> Телефон нөмірін дұрыс енгізіңіз (мысалы: 87071234567):")
        return

    # 2. Проверка дубликата в базе
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM clients WHERE phone = %s AND user_id != %s", (phone, message.from_user.id))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        await message.answer("⚠️ <b>Бұл нөмір жүйеде тіркеліп қойған!</b> Басқа нөмір енгізіңіз:")
        return

    data = await state.get_data()
    cursor.execute(
        "INSERT INTO clients (user_id, full_name, phone) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET full_name = EXCLUDED.full_name, phone = EXCLUDED.phone",
        (message.from_user.id, data.get('client_full_name', 'Клиент'), phone)
    )
    conn.commit()
    cursor.close()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Сіз сәтті тіркелдіңіз!</b>\n\nЕнді керекті бөлімді таңдай аласыз 👇", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")

# --- РЕГИСТРАЦИЯ ВОДИТЕЛЯ ---
@dp.message(F.text == "🚖 Жүргізуші болу")
async def start_driver_reg(message: types.Message, state: FSMContext):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drivers WHERE user_id = %s", (message.from_user.id,))
    driver = cursor.fetchone()
    cursor.close()
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
    await message.answer(
        "📱 <b>Байланыс телефоныңызды енгізіңіз:</b>\n\n"
        "Төмендегі «📱 Номерді жіберу» батырмасын басыңыз немесе қолдан жазыңыз (мысалы: 87071234567):",
        reply_markup=phone_request_menu(),
        parse_mode="HTML"
    )

@dp.message(DriverRegister.phone, F.contact | F.text)
async def process_driver_phone(message: types.Message, state: FSMContext):
    raw_phone = message.contact.phone_number if message.contact else message.text
    phone = validate_phone(raw_phone)

    # 1. Валидация формата
    if not phone:
        await message.answer("❌ <b>Қате нөмір!</b> Телефон нөмірін дұрыс енгізіңіз (мысалы: 87071234567):")
        return

    # 2. Проверка дубликата в базе
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM drivers WHERE phone = %s AND user_id != %s", (phone, message.from_user.id))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        await message.answer("⚠️ <b>Бұл нөмір басқа жүргізушіде тіркеліп қойған!</b> Басқа нөмір енгізіңіз:")
        return
    cursor.close()
    conn.close()

    await state.update_data(phone=phone)
    await state.set_state(DriverRegister.car_info)
    await message.answer("🚘 Көлігіңіздің маркасы мен мемлекеттік нөмірін жазыңыз:", reply_markup=cancel_menu())

@dp.message(DriverRegister.car_info)
async def process_driver_car(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO drivers (user_id, full_name, phone, car_info, balance) 
        VALUES (%s, %s, %s, %s, 0.0)
        ON CONFLICT (user_id) DO UPDATE 
        SET full_name = EXCLUDED.full_name, phone = EXCLUDED.phone, car_info = EXCLUDED.car_info
    ''', (message.from_user.id, data['full_name'], data['phone'], message.text))
    conn.commit()
    cursor.close()
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
        await message.answer("🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b>", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")

@dp.chat_join_request()
async def auto_approve_driver(chat_join_request: ChatJoinRequest):
    user_id = chat_join_request.from_user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drivers WHERE user_id = %s", (user_id,))
    driver = cursor.fetchone()
    cursor.close()
    conn.close()
    if driver:
        await chat_join_request.approve()
    else:
        await chat_join_request.decline()

# --- ЗАКАЗ ДОСТАВКИ ---
@dp.message(F.text == "📦 Жеткізу (Доставка)")
async def start_delivery_order(message: types.Message, state: FSMContext):
    if not await is_client_registered(message.from_user.id):
        await state.set_state(ClientRegister.full_name)
        await message.answer("👤 <b>Тапсырыс беру үшін алдымен тіркелу қажет!</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):", reply_markup=cancel_menu(), parse_mode="HTML")
        return

    await state.set_state(OrderDelivery.delivery_type)
    await message.answer("📦 <b>Жеткізу түрін таңдаңыз:</b>", reply_markup=delivery_menu(), parse_mode="HTML")

@dp.message(OrderDelivery.delivery_type, F.text.in_({"🏙 Қала ішінде жеткізу", "🛣 Қалааралық жеткізу"}))
async def process_delivery_type(message: types.Message, state: FSMContext):
    dtype = "city" if message.text == "🏙 Қала ішінде жеткізу" else "intercity"
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
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = %s", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Көрсетілмеген"

    cursor.execute(
        "INSERT INTO orders (user_id, order_type, delivery_type, item_info, from_loc, to_loc, price, phone, status) VALUES (%s, 'delivery', %s, %s, %s, %s, %s, %s, 'new') RETURNING id",
        (message.from_user.id, dtype, item_info, data['from_loc'], data['to_loc'], price, phone)
    )
    order_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Жеткізу тапсырысы қабылданды!</b> Жүргізуші іздестірілуде...", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")

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

# --- ЗАКАЗ ПО ГОРОДУ ---
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
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = %s", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Көрсетілмеген"

    cursor.execute(
        "INSERT INTO orders (user_id, order_type, from_loc, to_loc, price, phone, status) VALUES (%s, 'city', %s, %s, %s, %s, 'new') RETURNING id",
        (message.from_user.id, data['from_loc'], data['to_loc'], price, phone)
    )
    order_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Тапсырысыңыз қабылданды!</b> Жүргізуші іздестірілуде...", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛА ІШІНДЕ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {data['from_loc']}\n"
        f"🏁 <b>Қайда:</b> {data['to_loc']}\n"
        f"💰 <b>Жол ақысы:</b> {price}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]])
    await bot.send_message(chat_id=CITY_GROUP_ID, text=card_text, reply_markup=kb, parse_mode="HTML")

# --- ЗАКАЗ МЕЖГОРОД ---
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
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM clients WHERE user_id = %s", (message.from_user.id,))
    client = cursor.fetchone()
    phone = client[0] if client else "Көрсетілмеген"

    cursor.execute(
        "INSERT INTO orders (user_id, order_type, from_loc, to_loc, date_time, seats, price, phone, status) VALUES (%s, 'intercity', %s, %s, %s, %s, %s, %s, 'new') RETURNING id",
        (message.from_user.id, data['from_loc'], data['to_loc'], data['date_time'], data['seats'], price, phone)
    )
    order_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Қалааралық тапсырыс қабылданды!</b>", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")

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

# --- ПРИНЯТИЕ ЗАКАЗА ВОДИТЕЛЕМ ---
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback_query: types.CallbackQuery):
    order_id = int(callback_query.data.split('_')[1])
    driver = callback_query.from_user
    
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT full_name, phone, car_info, balance FROM drivers WHERE user_id = %s", (driver.id,))
    driver_db = cursor.fetchone()

    if not driver_db:
        await callback_query.answer("❌ Тапсырысты алу үшін алдымен ботта жүргізуші болып тіркеліңіз!", show_alert=True)
        cursor.close()
        conn.close()
        return

    cursor.execute("SELECT user_id, order_type, delivery_type, item_info, from_loc, to_loc, date_time, seats, price, phone, status FROM orders WHERE id = %s", (order_id,))
    order = cursor.fetchone()

    if not order or order[10] != 'new':
        await callback_query.answer("Тапсырыс бұрын алынған!", show_alert=True)
        cursor.close()
        conn.close()
        return

    price_val = parse_price(order[8])
    commission = (price_val * COMMISSION_PERCENT) / 100
    driver_balance = driver_db[3]

    if driver_balance < commission:
        await callback_query.answer(f"❌ Балансыңыз жеткіліксіз!\nТапсырыс комиссиясы: {commission:.0f} ₸\nБалансыңыз: {driver_balance:.0f} ₸\nБалансты толтырыңыз.", show_alert=True)
        cursor.close()
        conn.close()
        return

    new_balance = driver_balance - commission
    cursor.execute("UPDATE drivers SET balance = %s WHERE user_id = %s", (new_balance, driver.id))

    client_id = order[0]
    cursor.execute("SELECT full_name, phone FROM clients WHERE user_id = %s", (client_id,))
    client_db = cursor.fetchone()

    cursor.execute("UPDATE orders SET status = 'accepted' WHERE id = %s", (order_id,))
    conn.commit()
    cursor.close()
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
            f"💸 <b>Комиссия ұсталды:</b> {commission:.0f} ₸\n"
            f"💳 <b>Қалған баланс:</b> {new_balance:.0f} ₸\n"
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
            f"💸 <b>Комиссия ұсталды:</b> {commission:.0f} ₸\n"
            f"💳 <b>Қалған баланс:</b> {new_balance:.0f} ₸\n"
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

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())

