import os
import re
import html
import math
import time
import logging
import asyncio
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ChatJoinRequest,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# ======================= НАСТРОЙКИ (Environment Variables на Render) =======================
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не найдена!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").replace("@", "")

CITY_GROUP_ID = int(os.getenv("CITY_GROUP_ID", "-1004350443552"))
INTERCITY_GROUP_ID = int(os.getenv("INTERCITY_GROUP_ID", "-1003756709241"))

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL переменная окружения не найдена!")

COMMISSION_PERCENT = 10      # Процент комиссии от заказа
BONUS_AMOUNT = 5000          # Бонус новому водителю
MIN_PRICE = 100              # Минимальная цена заказа (защита от "договорная", "0" и т.п.)
MAX_ADMIN_AMOUNT = 1_000_000 # Максимум для ручного пополнения
ORDER_TIMEOUT_MINUTES = 15   # Через сколько минут непринятый заказ закрывается автоматически
REFUND_ON_CANCEL = False     # Комиссия водителю НЕ возвращается, если клиент отменил УЖЕ принятый заказ
KASPI_INFO = os.getenv("KASPI_INFO", "+7 775 699 63 09 (Еркебұлан Ә.)")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
# Все обработчики состояний (FSM) лежат в отдельном роутере, который подключается ПОСЛЕ
# основного. Благодаря этому кнопки меню работают в любом состоянии и не "съедаются" как ввод.
fsm = Router()


def esc(value) -> str:
    """Экранирование пользовательского текста для parse_mode=HTML."""
    return html.escape("" if value is None else str(value))


def clip(value, limit: int = 200) -> str:
    return (value or "").strip()[:limit]


# ======================= БАЗА ДАННЫХ (PostgreSQL, пул соединений) =======================
DB_POOL_MAX = 10
pool = ThreadedConnectionPool(
    1, DB_POOL_MAX, DATABASE_URL,
    sslmode="require",
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
)
_db_sem = threading.BoundedSemaphore(DB_POOL_MAX)


@contextmanager
def db_cursor():
    """Берёт соединение из пула, коммитит при успехе, откатывает при ошибке, всегда возвращает в пул."""
    with _db_sem:
        conn = pool.getconn()
        broken = False
        try:
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                broken = True
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        finally:
            pool.putconn(conn, close=broken or bool(conn.closed))


async def db(fn, *args, **kwargs):
    """Запускает синхронную функцию работы с БД в потоке (не блокирует бота).
    При обрыве соединения один раз повторяет попытку."""
    for attempt in (1, 2):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if attempt == 2:
                raise
            logging.warning(f"Обрыв соединения с БД, повтор: {e}")


def _init_db():
    with db_cursor() as cur:
        cur.execute('''
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
        # Новые колонки: кто принял заказ и когда создан (для учёта и разбора споров)
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_id BIGINT")
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
        # Для отмены заказов: где лежат карточки и сколько удержана комиссия
        for col in ("group_chat_id BIGINT", "group_message_id BIGINT", "group_text TEXT",
                    "driver_msg_id BIGINT", "driver_text TEXT", "client_msg_id BIGINT", "commission REAL"):
            cur.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col}")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS drivers (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                phone TEXT UNIQUE,
                car_info TEXT,
                balance REAL DEFAULT 0.0,
                has_received_bonus BOOLEAN DEFAULT FALSE
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                phone TEXT UNIQUE
            )
        ''')


def _is_driver(user_id):
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM drivers WHERE user_id = %s", (user_id,))
        return cur.fetchone() is not None


def _is_client(user_id):
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM clients WHERE user_id = %s", (user_id,))
        return cur.fetchone() is not None


def _get_driver(user_id):
    with db_cursor() as cur:
        cur.execute(
            "SELECT full_name, phone, car_info, balance, has_received_bonus FROM drivers WHERE user_id = %s",
            (user_id,),
        )
        return cur.fetchone()


def _get_stats():
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM clients")
        clients = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM drivers")
        drivers = cur.fetchone()[0]
        return clients, drivers


def _save_client(user_id, name, phone):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 FROM clients WHERE phone = %s AND user_id != %s", (phone, user_id))
            if cur.fetchone():
                return "dup"
            cur.execute(
                "INSERT INTO clients (user_id, full_name, phone) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET full_name = EXCLUDED.full_name, phone = EXCLUDED.phone",
                (user_id, name, phone),
            )
        return "ok"
    except psycopg2.IntegrityError:
        return "dup"


def _driver_phone_taken(phone, user_id):
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM drivers WHERE phone = %s AND user_id != %s", (phone, user_id))
        return cur.fetchone() is not None


def _save_driver(user_id, name, phone, car_info):
    try:
        with db_cursor() as cur:
            cur.execute('''
                INSERT INTO drivers (user_id, full_name, phone, car_info, balance)
                VALUES (%s, %s, %s, %s, 0.0)
                ON CONFLICT (user_id) DO UPDATE
                SET full_name = EXCLUDED.full_name, phone = EXCLUDED.phone, car_info = EXCLUDED.car_info
            ''', (user_id, name, phone, car_info))
        return "ok"
    except psycopg2.IntegrityError:
        return "dup"


def _create_order(user_id, order_type, delivery_type=None, item_info=None, from_loc=None,
                  to_loc=None, date_time=None, seats=None, price=None):
    with db_cursor() as cur:
        cur.execute("SELECT phone FROM clients WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        phone = row[0] if row else "Көрсетілмеген"
        cur.execute(
            "INSERT INTO orders (user_id, order_type, delivery_type, item_info, from_loc, to_loc, "
            "date_time, seats, price, phone, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new') RETURNING id",
            (user_id, order_type, delivery_type, item_info, from_loc, to_loc, date_time, seats, price, phone),
        )
        return cur.fetchone()[0]


def _set_order_status(order_id, status):
    with db_cursor() as cur:
        cur.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))


_ORDER_FIELDS = {"group_chat_id", "group_message_id", "group_text",
                 "driver_msg_id", "driver_text", "client_msg_id"}


def _update_order(order_id, **fields):
    """Сохраняет id сообщений/тексты карточек заказа (None пропускается)."""
    fields = {k: v for k, v in fields.items() if k in _ORDER_FIELDS and v is not None}
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    with db_cursor() as cur:
        cur.execute(f"UPDATE orders SET {sets} WHERE id = %s", (*fields.values(), order_id))


def _cancel_order(order_id, user_id):
    """Отмена заказа клиентом (только владелец, только пока заказ 'new' или 'accepted').
    Если заказ уже принят и включён REFUND_ON_CANCEL, комиссия возвращается водителю."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT status, driver_id, commission, group_chat_id, group_message_id, group_text, "
            "driver_msg_id, driver_text FROM orders WHERE id = %s AND user_id = %s FOR UPDATE",
            (order_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return {"status": "not_found"}
        (status, driver_id, commission, group_chat_id, group_message_id,
         group_text, driver_msg_id, driver_text) = row
        if status not in ('new', 'accepted'):
            return {"status": "already", "order_status": status}

        cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))

        refunded, new_balance = False, None
        commission = float(commission or 0)
        if status == 'accepted' and driver_id and REFUND_ON_CANCEL and commission > 0:
            cur.execute(
                "UPDATE drivers SET balance = COALESCE(balance, 0) + %s WHERE user_id = %s RETURNING balance",
                (commission, driver_id),
            )
            r = cur.fetchone()
            if r:
                refunded, new_balance = True, float(r[0])

        return {
            "status": "ok", "was_accepted": status == 'accepted', "driver_id": driver_id,
            "commission": commission, "refunded": refunded, "new_balance": new_balance,
            "group_chat_id": group_chat_id, "group_message_id": group_message_id, "group_text": group_text,
            "driver_msg_id": driver_msg_id, "driver_text": driver_text,
        }


def _expire_orders():
    """Закрывает заказы, которые никто не принял за ORDER_TIMEOUT_MINUTES."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = 'expired' "
            "WHERE status = 'new' AND created_at < NOW() - (%s * INTERVAL '1 minute') "
            "RETURNING id, user_id, group_chat_id, group_message_id, group_text, client_msg_id",
            (ORDER_TIMEOUT_MINUTES,),
        )
        return cur.fetchall()


def _topup(target_id, amount, bonus=False):
    """Атомарное пополнение: баланс меняется одним SQL-запросом, двойное начисление невозможно."""
    with db_cursor() as cur:
        if bonus:
            cur.execute(
                "UPDATE drivers SET balance = COALESCE(balance, 0) + %s, has_received_bonus = TRUE "
                "WHERE user_id = %s AND COALESCE(has_received_bonus, FALSE) = FALSE RETURNING balance",
                (amount, target_id),
            )
        else:
            cur.execute(
                "UPDATE drivers SET balance = COALESCE(balance, 0) + %s WHERE user_id = %s RETURNING balance",
                (amount, target_id),
            )
        row = cur.fetchone()
        if row:
            return {"status": "ok", "balance": float(row[0])}
        cur.execute("SELECT 1 FROM drivers WHERE user_id = %s", (target_id,))
        return {"status": "bonus_taken" if cur.fetchone() else "no_driver"}


class _InsufficientBalance(Exception):
    def __init__(self, commission, balance):
        self.commission = commission
        self.balance = balance


def _accept_order(order_id, driver_id):
    """Принятие заказа одной транзакцией:
    - заказ достаётся только одному водителю (UPDATE ... WHERE status='new');
    - комиссия списывается атомарно; если денег нет, всё откатывается и заказ остаётся 'new'."""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT full_name, phone, car_info FROM drivers WHERE user_id = %s", (driver_id,))
            drv = cur.fetchone()
            if not drv:
                return {"status": "not_driver"}

            cur.execute(
                "UPDATE orders SET status = 'accepted', driver_id = %s "
                "WHERE id = %s AND status = 'new' AND user_id <> %s "
                "RETURNING user_id, order_type, delivery_type, item_info, from_loc, to_loc, "
                "date_time, seats, price, phone, client_msg_id",
                (driver_id, order_id, driver_id),
            )
            order = cur.fetchone()
            if not order:
                cur.execute("SELECT user_id, status FROM orders WHERE id = %s", (order_id,))
                o = cur.fetchone()
                if o and o[1] == 'new' and o[0] == driver_id:
                    return {"status": "own"}
                if o and o[1] in ('cancelled', 'expired'):
                    return {"status": o[1]}
                return {"status": "taken"}

            commission = round(parse_price(order[8]) * COMMISSION_PERCENT / 100)
            cur.execute(
                "UPDATE drivers SET balance = balance - %s WHERE user_id = %s AND balance >= %s RETURNING balance",
                (commission, driver_id, commission),
            )
            bal = cur.fetchone()
            if not bal:
                cur.execute("SELECT balance FROM drivers WHERE user_id = %s", (driver_id,))
                raise _InsufficientBalance(commission, float(cur.fetchone()[0] or 0))

            cur.execute("UPDATE orders SET commission = %s WHERE id = %s", (commission, order_id))
            cur.execute("SELECT full_name, phone FROM clients WHERE user_id = %s", (order[0],))
            client = cur.fetchone()

            return {
                "status": "ok",
                "driver": drv,
                "client_id": order[0],
                "client": client,
                "order": order,
                "commission": commission,
                "new_balance": float(bal[0]),
            }
    except _InsufficientBalance as e:
        return {"status": "no_balance", "commission": e.commission, "balance": e.balance}


# ======================= FSM (СОСТОЯНИЯ) =======================
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


# ======================= ВАЛИДАЦИЯ =======================
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


_PRICE_RE = re.compile(r'\d{1,3}(?:[ \u00a0]\d{3})+|\d+')


def parse_price(price_str: str) -> float:
    """'5000' -> 5000, '5 000 тг' -> 5000, '5к' -> 5000, '2000-3000' -> 2000 (раньше было 20003000!)."""
    text = str(price_str).lower()
    m = _PRICE_RE.search(text)
    if not m:
        return 0.0
    value = float(re.sub(r'\D', '', m.group(0)))
    rest = text[m.end():].lstrip()
    if rest.startswith(('к', 'k')) and value < 1000:
        value *= 1000
    return value


# ======================= МЕНЮ =======================
async def is_driver_registered(user_id: int) -> bool:
    try:
        return await db(_is_driver, user_id)
    except Exception as e:
        logging.error(f"Ошибка проверки водителя: {e}")
        return False


async def is_client_registered(user_id: int) -> bool:
    try:
        return await db(_is_client, user_id)
    except Exception as e:
        logging.error(f"Ошибка проверки клиента: {e}")
        return False


async def main_menu(user_id: int = None):
    keyboard = [
        [KeyboardButton(text="🏙 Қала ішінде"), KeyboardButton(text="🛣 Қалааралық (Межгород)")],
        [KeyboardButton(text="📦 Жеткізу (Доставка)"), KeyboardButton(text="🚖 Жүргізуші болу")],
        [KeyboardButton(text="📞 Қолдау қызметі (Админге жазу)")],
    ]
    if user_id and await is_driver_registered(user_id):
        keyboard.append([KeyboardButton(text="💰 Менің балансым"), KeyboardButton(text="💳 Баланс толтыру")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def delivery_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏙 Қала ішінде жеткізу"), KeyboardButton(text="🛣 Қалааралық жеткізу")],
            [KeyboardButton(text="❌ Бас тарту")],
        ],
        resize_keyboard=True,
    )


def cancel_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Бас тарту")]], resize_keyboard=True)


def phone_request_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Номерді жіберу", request_contact=True)],
            [KeyboardButton(text="❌ Бас тарту")],
        ],
        resize_keyboard=True,
    )


def cancel_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Тапсырысты жою", callback_data=f"cancelorder_{order_id}")]
    ])


async def safe_edit(chat_id: int, message_id: int, text: str):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                    reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось изменить сообщение {message_id} в чате {chat_id}: {e}")


async def safe_remove_buttons(chat_id: int, message_id: int):
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception as e:
        logging.warning(f"Не удалось убрать кнопки у сообщения {message_id}: {e}")


# ======================= ССЫЛКИ НА ГРУППЫ =======================
_invite_links: dict[int, str] = {}


async def get_join_link(chat_id: int) -> str:
    """Ссылка С ЗАЯВКОЙ на вступление (creates_join_request=True), создаётся один раз и кешируется.
    Раньше использовался export_chat_invite_link: он каждый раз ПЕРЕВЫПУСКАЕТ основную ссылку
    (старые ссылки умирают) и не требует одобрения, поэтому проверка водителя не срабатывала."""
    if chat_id not in _invite_links:
        link = await bot.create_chat_invite_link(chat_id, name="drivers", creates_join_request=True)
        _invite_links[chat_id] = link.invite_link
    return _invite_links[chat_id]


async def driver_groups_kb() -> InlineKeyboardMarkup:
    city_link = await get_join_link(CITY_GROUP_ID)
    intercity_link = await get_join_link(INTERCITY_GROUP_ID)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏙 Қала ішіндегі топқа қосылу", url=city_link)],
        [InlineKeyboardButton(text="🛣 Қалааралық топқа қосылу", url=intercity_link)],
    ])


# ======================= СТАРТ / ОТМЕНА / ПОДДЕРЖКА =======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    name = esc(message.from_user.first_name)
    if await is_client_registered(message.from_user.id):
        text = (
            f"Ассалаумағалейкум, <b>{name}</b>!\n\n"
            f"🚖 <b>«Жолдас такси»</b> ботына қош келдіңіз!\n\n"
            f"Керекті бөлімді таңдаңыз 👇"
        )
        await message.answer(text, reply_markup=await main_menu(message.from_user.id), parse_mode="HTML")
    else:
        await state.set_state(ClientRegister.full_name)
        text = (
            f"Ассалаумағалейкум, <b>{name}</b>!\n\n"
            f"🚖 <b>«Жолдас такси»</b> қызметін пайдалану үшін алдымен тіркелу қажет.\n\n"
            f"👤 <b>Аты-жөніңізді жазыңыз (ФИО):</b>"
        )
        await message.answer(text, reply_markup=cancel_menu(), parse_mode="HTML")


@dp.message(Command("stats"))
async def admin_get_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        clients_count, drivers_count = await db(_get_stats)
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


@dp.message(F.text.in_({"❌ Бас тарту", "⬅️ Қайту (артқа)"}))
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_client_registered(message.from_user.id):
        await message.answer("❌ Тоқтатылды.", reply_markup=await main_menu(message.from_user.id))
    else:
        await message.answer("❌ Тоқтатылды. Қайта бастау үшін /start басыңыз.", reply_markup=types.ReplyKeyboardRemove())


@dp.message(F.text == "📞 Қолдау қызметі (Админге жазу)")
async def support_contact(message: types.Message, state: FSMContext):
    await state.clear()
    url = f"https://t.me/{ADMIN_USERNAME}" if ADMIN_USERNAME else f"tg://user?id={ADMIN_ID}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Админге жазу (ЛС)", url=url)]])
    text = (
        "📞 <b>Қолдау қызметі</b>\n\n"
        "Сұрақтарыңыз немесе ұсыныстарыңыз болса, төмендегі батырманы басып админге жаза аласыз 👇"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ======================= БАЛАНС И ПОПОЛНЕНИЕ ВОДИТЕЛЯ =======================
@dp.message(F.text == "💰 Менің балансым")
async def show_driver_balance(message: types.Message, state: FSMContext):
    await state.clear()
    driver = await db(_get_driver, message.from_user.id)
    if driver:
        text = (
            f"💰 <b>Сіздің балансыңыз:</b> {float(driver[3] or 0):.0f} ₸\n\n"
            f"📌 Балансты толтыру үшін «💳 Баланс толтыру» батырмасын басып, Kaspi чегін жіберіңіз."
        )
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer("❌ Сіз жүргізуші ретінде тіркелмегенсіз.")


@dp.message(F.text == "💳 Баланс толтыру")
async def request_topup_receipt(message: types.Message, state: FSMContext):
    await state.clear()
    if not await is_driver_registered(message.from_user.id):
        await message.answer("❌ Сіз жүргізуші ретінде тіркелмегенсіз.")
        return
    await state.set_state(TopupState.waiting_for_receipt)
    text = (
        f"💳 <b>Балансты Kaspi арқылы толтыру:</b>\n\n"
        f"1. Kaspi арқылы мына номерге аударыңыз: <b>{esc(KASPI_INFO)}</b>\n"
        f"2. Төлем жасап болған соң, <b>чектің суретін (скриншот) немесе файлын дәл осы чатқа жіберіңіз</b> 👇"
    )
    await message.answer(text, reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(TopupState.waiting_for_receipt, F.photo | F.document)
async def process_topup_receipt(message: types.Message, state: FSMContext):
    driver_id = message.from_user.id
    driver = await db(_get_driver, driver_id)

    driver_name = driver[0] if driver else message.from_user.full_name
    driver_phone = driver[1] if driver else "Нет номера"
    has_bonus = bool(driver[4]) if driver else False

    bonus_status = (
        f"⚠️ УЖЕ ПОЛУЧАЛ {BONUS_AMOUNT} ₸ БОНУС" if has_bonus
        else f"🎁 ЕЩЕ НЕ ПОЛУЧАЛ БОНУС (МОЖНО ДАТЬ {BONUS_AMOUNT} ₸)"
    )

    caption = (
        f"💳 <b>ЖАҢА ТӨЛЕМ ЧЕГІ!</b>\n\n"
        f"👤 <b>Жүргізуші:</b> {esc(driver_name)}\n"
        f"📞 <b>Тел:</b> {esc(driver_phone)}\n"
        f"🆔 <b>ID:</b> <code>{driver_id}</code>\n"
        f"📌 <b>Статус бонуса:</b> {bonus_status}\n\n"
        f"Толтырылатын сумманы таңдаңыз 👇"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 БОНУС {BONUS_AMOUNT} ₸", callback_data=f"paybonus_{driver_id}")],
        [
            InlineKeyboardButton(text="➕ 500 ₸", callback_data=f"pay_{driver_id}_500"),
            InlineKeyboardButton(text="➕ 1000 ₸", callback_data=f"pay_{driver_id}_1000"),
        ],
        [
            InlineKeyboardButton(text="➕ 2000 ₸", callback_data=f"pay_{driver_id}_2000"),
            InlineKeyboardButton(text="➕ 5000 ₸", callback_data=f"pay_{driver_id}_5000"),
        ],
        [InlineKeyboardButton(text="✏️ Басқа сумма", callback_data=f"paycustom_{driver_id}")],
    ])

    try:
        if message.photo:
            await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=caption,
                                 reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_document(chat_id=ADMIN_ID, document=message.document.file_id, caption=caption,
                                    reply_markup=kb, parse_mode="HTML")
        await state.clear()
        await message.answer(
            "✅ <b>Чек администраторға жіберілді!</b> Балансыңыз жақын арада толтырылады.",
            reply_markup=await main_menu(driver_id), parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Ошибка отправки чека админу: {e}")
        await message.answer("❌ Чекті жіберу кезінде қате шықты. Админге хабарласыңыз.")


# ======================= ОБРАБОТКА ПОПОЛНЕНИЯ АДМИНОМ =======================
_processed_receipts: set = set()  # защита от двойного нажатия на одну и ту же кнопку


def topup_notice(old_balance: float, amount: float, new_balance: float, bonus: bool = False) -> str:
    if bonus:
        return (
            f"🎉 <b>Сізге {amount:.0f} ₸ ашылу бонусы берілді!</b>\n\n"
            f"💰 <b>Бұрынғы баланс:</b> {old_balance:.0f} ₸\n"
            f"🎁 <b>Бонус:</b> {amount:.0f} ₸\n"
            f"💵 <b>Ағымдағы баланс:</b> {new_balance:.0f} ₸"
        )
    if amount >= 0:
        title, line = "🎉 <b>Балансыңыз толтырылды!</b>", f"➕ <b>Қосылды:</b> {amount:.0f} ₸"
    else:
        title, line = "ℹ️ <b>Балансыңыз түзетілді</b>", f"➖ <b>Алынды:</b> {abs(amount):.0f} ₸"
    return (
        f"{title}\n\n"
        f"💰 <b>Бұрынғы баланс:</b> {old_balance:.0f} ₸\n"
        f"{line}\n"
        f"💵 <b>Ағымдағы баланс:</b> {new_balance:.0f} ₸"
    )


async def notify_driver(target_id: int, text: str):
    try:
        await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление водителю {target_id}: {e}")


async def admin_credit(callback_query: types.CallbackQuery, target_id: int, amount: float, bonus: bool):
    key = (callback_query.message.chat.id, callback_query.message.message_id)
    if key in _processed_receipts:
        await callback_query.answer("⏳ Бұл чек өңделіп жатыр немесе өңделіп қойған.", show_alert=True)
        return
    _processed_receipts.add(key)

    result = await db(_topup, target_id, amount, bonus)

    if result["status"] == "no_driver":
        _processed_receipts.discard(key)
        await callback_query.answer("❌ Жүргізуші табылмады!", show_alert=True)
        return
    if result["status"] == "bonus_taken":
        _processed_receipts.discard(key)
        await callback_query.answer(f"⚠️ Бұл жүргізуші {BONUS_AMOUNT} ₸ бонусты бұрын алып қойған!", show_alert=True)
        return

    new_balance = result["balance"]
    old_balance = new_balance - amount

    label = f"🎁 <b>БОНУС {amount:.0f} ₸ БЕРІЛДІ!</b>" if bonus else f"✅ <b>ТОЛТЫРЫЛДЫ: +{amount:.0f} ₸</b>"
    try:
        await callback_query.message.edit_caption(
            caption=callback_query.message.html_text + f"\n\n{label}\nБаланс: {new_balance:.0f} ₸",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Не удалось обновить сообщение с чеком: {e}")

    await notify_driver(target_id, topup_notice(old_balance, amount, new_balance, bonus))
    await callback_query.answer("Дайын!")


@dp.callback_query(F.data.startswith("paybonus_"))
async def admin_bonus_pay(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("⛔ Рұқсат жоқ", show_alert=True)
        return
    target_id = int(callback_query.data.split("_")[1])
    await admin_credit(callback_query, target_id, float(BONUS_AMOUNT), bonus=True)


@dp.callback_query(F.data.startswith("pay_"))
async def admin_quick_pay(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("⛔ Рұқсат жоқ", show_alert=True)
        return
    parts = callback_query.data.split("_")
    await admin_credit(callback_query, int(parts[1]), float(parts[2]), bonus=False)


@dp.callback_query(F.data.startswith("paycustom_"))
async def admin_custom_pay_start(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("⛔ Рұқсат жоқ", show_alert=True)
        return
    target_id = int(callback_query.data.split("_")[1])
    await state.update_data(target_driver_id=target_id)
    await state.set_state(TopupState.waiting_custom_amount)
    await callback_query.message.answer(
        f"✍️ ID {target_id} жүргізушіге қанша сумма қосқыңыз келеді? (тек сан жазыңыз, мысалы: 1500)",
        reply_markup=cancel_menu(),
    )
    await callback_query.answer()


def parse_admin_amount(text: str) -> float | None:
    try:
        amount = float((text or "").strip().replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(amount) or amount == 0 or abs(amount) > MAX_ADMIN_AMOUNT:
        return None
    return amount


@fsm.message(TopupState.waiting_custom_amount, F.text)
async def admin_custom_pay_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    amount = parse_admin_amount(message.text)
    if amount is None:
        await message.answer(f"❌ Тек сан енгізіңіз (0-ден өзгеше, {MAX_ADMIN_AMOUNT} ₸-ден аспайтын)! Мысалы: 1500")
        return

    data = await state.get_data()
    target_id = data.get("target_driver_id")
    result = await db(_topup, target_id, amount, False)
    await state.clear()

    if result["status"] != "ok":
        await message.answer("❌ Жүргізуші табылмады!", reply_markup=await main_menu(message.from_user.id))
        return

    new_balance = result["balance"]
    old_balance = new_balance - amount
    await message.answer(
        f"✅ ID {target_id} жүргізушіге {amount:.0f} ₸ өзгертілді!\n"
        f"Бұрын: {old_balance:.0f} ₸ ➔ Қазір: {new_balance:.0f} ₸",
        reply_markup=await main_menu(message.from_user.id),
    )
    await notify_driver(target_id, topup_notice(old_balance, amount, new_balance))


@dp.message(Command("pay"))
async def admin_topup_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        args = message.text.split()
        target_id = int(args[1])
        amount = parse_admin_amount(args[2])
        if amount is None:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("❌ Формат қате! Қолдану: `/pay USER_ID SUMMA`", parse_mode="Markdown")
        return

    result = await db(_topup, target_id, amount, False)
    if result["status"] != "ok":
        await message.answer("❌ Жүргізуші табылмады!")
        return

    new_balance = result["balance"]
    old_balance = new_balance - amount
    await message.answer(f"✅ ID {target_id} жүргізушіге {amount:.0f} ₸ өзгертілді! Жаңа баланс: {new_balance:.0f} ₸")
    await notify_driver(target_id, topup_notice(old_balance, amount, new_balance))


# ======================= РЕГИСТРАЦИЯ КЛИЕНТА =======================
@fsm.message(ClientRegister.full_name, F.text)
async def process_client_name(message: types.Message, state: FSMContext):
    await state.update_data(client_full_name=clip(message.text, 100))
    await state.set_state(ClientRegister.phone)
    await message.answer(
        "📱 <b>Байланыс телефоныңызды енгізіңіз:</b>\n\n"
        "Төмендегі «📱 Номерді жіберу» батырмасын басыңыз немесе қолдан жазыңыз (мысалы: 87071234567):",
        reply_markup=phone_request_menu(),
        parse_mode="HTML",
    )


@fsm.message(ClientRegister.phone, F.contact | F.text)
async def process_client_phone(message: types.Message, state: FSMContext):
    if message.contact and message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("❌ Тек өз нөміріңізді жіберіңіз (📱 Номерді жіберу батырмасы).")
        return

    raw_phone = message.contact.phone_number if message.contact else message.text
    phone = validate_phone(raw_phone)
    if not phone:
        await message.answer("❌ <b>Қате нөмір!</b> Телефон нөмірін дұрыс енгізіңіз (мысалы: 87071234567):", parse_mode="HTML")
        return

    data = await state.get_data()
    result = await db(_save_client, message.from_user.id, data.get('client_full_name', 'Клиент'), phone)
    if result == "dup":
        await message.answer("⚠️ <b>Бұл нөмір жүйеде тіркеліп қойған!</b> Басқа нөмір енгізіңіз:", parse_mode="HTML")
        return

    await state.clear()
    await message.answer(
        "✅ <b>Сіз сәтті тіркелдіңіз!</b>\n\nЕнді керекті бөлімді таңдай аласыз 👇",
        reply_markup=await main_menu(message.from_user.id), parse_mode="HTML",
    )


# ======================= РЕГИСТРАЦИЯ ВОДИТЕЛЯ =======================
@dp.message(F.text == "🚖 Жүргізуші болу")
async def start_driver_reg(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_driver_registered(message.from_user.id):
        try:
            kb = await driver_groups_kb()
            await message.answer(
                "✅ <b>Сіз тіркелген жүргізушісіз!</b> Топтарға өту үшін төмендегі сілтемелерді басыңыз:",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception as e:
            logging.error(f"Ошибка получения ссылок: {e}")
            await message.answer("✅ <b>Сіз тіркелген жүргізушісіз!</b> Жұмыс топтарына қосылыңыз.", parse_mode="HTML")
        return

    await state.set_state(DriverRegister.full_name)
    await message.answer(
        "📝 <b>Жүргізуші болып тіркелу</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):",
        reply_markup=cancel_menu(), parse_mode="HTML",
    )


@fsm.message(DriverRegister.full_name, F.text)
async def process_driver_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=clip(message.text, 100))
    await state.set_state(DriverRegister.phone)
    await message.answer(
        "📱 <b>Байланыс телефоныңызды енгізіңіз:</b>\n\n"
        "Төмендегі «📱 Номерді жіберу» батырмасын басыңыз немесе қолдан жазыңыз (мысалы: 87071234567):",
        reply_markup=phone_request_menu(),
        parse_mode="HTML",
    )


@fsm.message(DriverRegister.phone, F.contact | F.text)
async def process_driver_phone(message: types.Message, state: FSMContext):
    if message.contact and message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("❌ Тек өз нөміріңізді жіберіңіз (📱 Номерді жіберу батырмасы).")
        return

    raw_phone = message.contact.phone_number if message.contact else message.text
    phone = validate_phone(raw_phone)
    if not phone:
        await message.answer("❌ <b>Қате нөмір!</b> Телефон нөмірін дұрыс енгізіңіз (мысалы: 87071234567):", parse_mode="HTML")
        return

    if await db(_driver_phone_taken, phone, message.from_user.id):
        await message.answer("⚠️ <b>Бұл нөмір басқа жүргізушіде тіркеліп қойған!</b> Басқа нөмір енгізіңіз:", parse_mode="HTML")
        return

    await state.update_data(phone=phone)
    await state.set_state(DriverRegister.car_info)
    await message.answer("🚘 Көлігіңіздің маркасы мен мемлекеттік нөмірін жазыңыз:", reply_markup=cancel_menu())


@fsm.message(DriverRegister.car_info, F.text)
async def process_driver_car(message: types.Message, state: FSMContext):
    data = await state.get_data()
    result = await db(_save_driver, message.from_user.id, data.get('full_name', 'Жүргізуші'),
                      data.get('phone'), clip(message.text, 100))
    if result == "dup":
        await state.set_state(DriverRegister.phone)
        await message.answer("⚠️ <b>Бұл нөмір басқа жүргізушіде тіркеліп қойған!</b> Басқа нөмір енгізіңіз:",
                             reply_markup=phone_request_menu(), parse_mode="HTML")
        return

    await state.clear()
    try:
        kb = await driver_groups_kb()
        await message.answer("🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b>\n\nТоптарға қосылыңыз 👇", reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка сгенерировать ссылки: {e}")
        await message.answer("🎉 <b>Құттықтаймыз! Тіркелдіңіз.</b>",
                             reply_markup=await main_menu(message.from_user.id), parse_mode="HTML")


@dp.chat_join_request()
async def auto_approve_driver(chat_join_request: ChatJoinRequest):
    """Принимает в группы только зарегистрированных водителей."""
    try:
        if await is_driver_registered(chat_join_request.from_user.id):
            await chat_join_request.approve()
        else:
            await chat_join_request.decline()
    except Exception as e:
        logging.error(f"Ошибка обработки заявки в группу: {e}")


# ======================= ОБЩЕЕ: ПУБЛИКАЦИЯ ЗАКАЗА В ГРУППУ =======================
async def publish_order(message: types.Message, order_id: int, group_id: int, card_text: str, ok_text: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚖 Тапсырысты алу", callback_data=f"accept_{order_id}")]
    ])
    try:
        sent = await bot.send_message(chat_id=group_id, text=card_text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить заказ №{order_id} в группу: {e}")
        await db(_set_order_status, order_id, "error")
        await message.answer("❌ Тапсырысты жіберу кезінде қате шықты. Админге хабарласыңыз.",
                             reply_markup=await main_menu(message.from_user.id))
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ Заказ №{order_id} не удалось отправить в группу {group_id}: {str(e)[:300]}")
        except Exception:
            pass
        return
    await db(_update_order, order_id, group_chat_id=group_id, group_message_id=sent.message_id, group_text=card_text)

    await message.answer(ok_text, reply_markup=await main_menu(message.from_user.id), parse_mode="HTML")
    status_msg = await message.answer(
        f"📋 <b>Тапсырыс №{order_id}</b>\n"
        f"⏳ Егер {ORDER_TIMEOUT_MINUTES} минут ішінде жүргізуші табылмаса, тапсырыс автоматты түрде жойылады.\n\n"
        f"Тапсырысты жою үшін төмендегі батырманы басыңыз 👇",
        reply_markup=cancel_order_kb(order_id), parse_mode="HTML",
    )
    await db(_update_order, order_id, client_msg_id=status_msg.message_id)


async def require_client(message: types.Message, state: FSMContext) -> bool:
    """True если клиент зарегистрирован; иначе запускает регистрацию."""
    if await is_client_registered(message.from_user.id):
        return True
    await state.set_state(ClientRegister.full_name)
    await message.answer(
        "👤 <b>Тапсырыс беру үшін алдымен тіркелу қажет!</b>\n\nТолық аты-жөніңізді жазыңыз (ФИО):",
        reply_markup=cancel_menu(), parse_mode="HTML",
    )
    return False


PRICE_ERROR = "❌ <b>Бағаны санмен жазыңыз</b> (мысалы: 1500):"


# ======================= ЗАКАЗ ДОСТАВКИ =======================
@dp.message(F.text == "📦 Жеткізу (Доставка)")
async def start_delivery_order(message: types.Message, state: FSMContext):
    await state.clear()
    if not await require_client(message, state):
        return
    await state.set_state(OrderDelivery.delivery_type)
    await message.answer("📦 <b>Жеткізу түрін таңдаңыз:</b>", reply_markup=delivery_menu(), parse_mode="HTML")


@fsm.message(OrderDelivery.delivery_type, F.text.in_({"🏙 Қала ішінде жеткізу", "🛣 Қалааралық жеткізу"}))
async def process_delivery_type(message: types.Message, state: FSMContext):
    dtype = "city" if message.text == "🏙 Қала ішінде жеткізу" else "intercity"
    await state.update_data(delivery_type=dtype)
    await state.set_state(OrderDelivery.item_info)
    await message.answer("📦 <b>Не жеткізу керек?</b>\n(Мысалы: Документы, Коробка 5кг, Ключи и т.д.):",
                         reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderDelivery.item_info, F.text)
async def process_delivery_item(message: types.Message, state: FSMContext):
    await state.update_data(item_info=clip(message.text))
    await state.set_state(OrderDelivery.from_loc)
    await message.answer("📍 <b>Затты қай жерден алып кету керек?</b> (Адрес / объект):",
                         reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderDelivery.from_loc, F.text)
async def process_delivery_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=clip(message.text))
    await state.set_state(OrderDelivery.to_loc)
    await message.answer("🏁 <b>Қай жерге жеткізіп беру керек?</b> (Адрес / объект):",
                         reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderDelivery.to_loc, F.text)
async def process_delivery_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=clip(message.text))
    await state.set_state(OrderDelivery.price)
    await message.answer("💰 <b>Жеткізу ақысын қанша ұсынасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderDelivery.price, F.text)
async def process_delivery_price(message: types.Message, state: FSMContext):
    price = clip(message.text, 50)
    if parse_price(price) < MIN_PRICE:
        await message.answer(PRICE_ERROR, reply_markup=cancel_menu(), parse_mode="HTML")
        return

    data = await state.get_data()
    dtype = data.get('delivery_type', 'city')
    item_info = data.get('item_info', 'Зат / Посылка')

    order_id = await db(_create_order, message.from_user.id, 'delivery', delivery_type=dtype,
                        item_info=item_info, from_loc=data['from_loc'], to_loc=data['to_loc'], price=price)
    await state.clear()

    if dtype == "city":
        type_title, group_id = "ҚАЛА ІШІНДЕ", CITY_GROUP_ID
    else:
        type_title, group_id = "ҚАЛААРАЛЫҚ (МЕЖГОРОД)", INTERCITY_GROUP_ID

    card_text = (
        f"📦 <b>ЖЕТКІЗУ (ДОСТАВКА - {type_title}) №{order_id}</b>\n\n"
        f"📦 <b>Зат / Посылка:</b> {esc(item_info)}\n"
        f"📍 <b>Қайдан:</b> {esc(data['from_loc'])}\n"
        f"🏁 <b>Қайда:</b> {esc(data['to_loc'])}\n"
        f"💰 <b>Жеткізу ақысы:</b> {esc(price)}\n"
    )
    await publish_order(message, order_id, group_id, card_text,
                        "✅ <b>Жеткізу тапсырысы қабылданды!</b> Жүргізуші іздестірілуде...")


# ======================= ЗАКАЗ ПО ГОРОДУ =======================
@dp.message(F.text == "🏙 Қала ішінде")
async def start_city_order(message: types.Message, state: FSMContext):
    await state.clear()
    if not await require_client(message, state):
        return
    await state.set_state(OrderCity.from_loc)
    await message.answer("📍 <b>Қайдан алып кетейік?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderCity.from_loc, F.text)
async def process_city_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=clip(message.text))
    await state.set_state(OrderCity.to_loc)
    await message.answer("🏁 <b>Қайда барамыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderCity.to_loc, F.text)
async def process_city_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=clip(message.text))
    await state.set_state(OrderCity.price)
    await message.answer("💰 <b>Жол ақысын қанша ұсынасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderCity.price, F.text)
async def process_city_price(message: types.Message, state: FSMContext):
    price = clip(message.text, 50)
    if parse_price(price) < MIN_PRICE:
        await message.answer(PRICE_ERROR, reply_markup=cancel_menu(), parse_mode="HTML")
        return

    data = await state.get_data()
    order_id = await db(_create_order, message.from_user.id, 'city',
                        from_loc=data['from_loc'], to_loc=data['to_loc'], price=price)
    await state.clear()

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛА ІШІНДЕ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {esc(data['from_loc'])}\n"
        f"🏁 <b>Қайда:</b> {esc(data['to_loc'])}\n"
        f"💰 <b>Жол ақысы:</b> {esc(price)}\n"
    )
    await publish_order(message, order_id, CITY_GROUP_ID, card_text,
                        "✅ <b>Тапсырысыңыз қабылданды!</b> Жүргізуші іздестірілуде...")


# ======================= ЗАКАЗ МЕЖГОРОД =======================
@dp.message(F.text == "🛣 Қалааралық (Межгород)")
async def start_intercity_order(message: types.Message, state: FSMContext):
    await state.clear()
    if not await require_client(message, state):
        return
    await state.set_state(OrderIntercity.from_loc)
    await message.answer("📍 <b>Қай қаладан / ауылдан шығасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderIntercity.from_loc, F.text)
async def process_inter_from(message: types.Message, state: FSMContext):
    await state.update_data(from_loc=clip(message.text))
    await state.set_state(OrderIntercity.to_loc)
    await message.answer("🏁 <b>Қай қалаға / ауылға барасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderIntercity.to_loc, F.text)
async def process_inter_to(message: types.Message, state: FSMContext):
    await state.update_data(to_loc=clip(message.text))
    await state.set_state(OrderIntercity.date_time)
    await message.answer("📅 <b>Қай күні және сағат қаншада?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderIntercity.date_time, F.text)
async def process_inter_time(message: types.Message, state: FSMContext):
    await state.update_data(date_time=clip(message.text, 100))
    await state.set_state(OrderIntercity.seats)
    await message.answer("👥 <b>Қанша орын керек?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderIntercity.seats, F.text)
async def process_inter_seats(message: types.Message, state: FSMContext):
    await state.update_data(seats=clip(message.text, 50))
    await state.set_state(OrderIntercity.price)
    await message.answer("💰 <b>Жол ақысын қанша ұсынасыз?</b>", reply_markup=cancel_menu(), parse_mode="HTML")


@fsm.message(OrderIntercity.price, F.text)
async def process_inter_price(message: types.Message, state: FSMContext):
    price = clip(message.text, 50)
    if parse_price(price) < MIN_PRICE:
        await message.answer(PRICE_ERROR, reply_markup=cancel_menu(), parse_mode="HTML")
        return

    data = await state.get_data()
    order_id = await db(_create_order, message.from_user.id, 'intercity', from_loc=data['from_loc'],
                        to_loc=data['to_loc'], date_time=data['date_time'], seats=data['seats'], price=price)
    await state.clear()

    card_text = (
        f"🚨 <b>ЖОЛДАС ТАКСИ: ҚАЛААРАЛЫҚ №{order_id}</b>\n\n"
        f"📍 <b>Қайдан:</b> {esc(data['from_loc'])}\n"
        f"🏁 <b>Қайда:</b> {esc(data['to_loc'])}\n"
        f"📅 <b>Уақыты:</b> {esc(data['date_time'])}\n"
        f"👥 <b>Орын:</b> {esc(data['seats'])}\n"
        f"💰 <b>Жол ақысы:</b> {esc(price)}\n"
    )
    await publish_order(message, order_id, INTERCITY_GROUP_ID, card_text,
                        "✅ <b>Қалааралық тапсырыс қабылданды!</b>")


# ======================= ПРИНЯТИЕ ЗАКАЗА ВОДИТЕЛЕМ =======================
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback_query: types.CallbackQuery):
    order_id = int(callback_query.data.split('_')[1])
    driver = callback_query.from_user

    res = await db(_accept_order, order_id, driver.id)

    if res["status"] == "not_driver":
        await callback_query.answer("❌ Тапсырысты алу үшін алдымен ботта жүргізуші болып тіркеліңіз!", show_alert=True)
        return
    if res["status"] == "taken":
        await callback_query.answer("Тапсырыс бұрын алынған!", show_alert=True)
        return
    if res["status"] == "own":
        await callback_query.answer("❌ Өз тапсырысыңызды ала алмайсыз!", show_alert=True)
        return
    if res["status"] == "cancelled":
        await callback_query.answer("❌ Клиент тапсырысты жойды!", show_alert=True)
        return
    if res["status"] == "expired":
        await callback_query.answer("⌛ Тапсырыстың уақыты өтіп кетті!", show_alert=True)
        return
    if res["status"] == "no_balance":
        await callback_query.answer(
            f"❌ Балансыңыз жеткіліксіз!\nТапсырыс комиссиясы: {res['commission']:.0f} ₸\n"
            f"Балансыңыз: {res['balance']:.0f} ₸\nБалансты толтырыңыз.",
            show_alert=True,
        )
        return

    # ---- успех ----
    order = res["order"]
    commission = res["commission"]
    new_balance = res["new_balance"]
    client_id = res["client_id"]
    client_db = res["client"]
    driver_db = res["driver"]

    (_, order_type, delivery_type, item_info, from_loc, to_loc,
     date_time, seats, price, raw_client_phone, prev_client_msg_id) = order

    clean_client_phone = clean_phone_number(raw_client_phone)
    client_name = client_db[0] if client_db else "Клиент"
    driver_name = driver_db[0] or driver.full_name
    driver_phone = driver_db[1] or "Көрсетілмеген"
    clean_driver_phone = clean_phone_number(driver_phone)
    driver_car = driver_db[2] or "Көрсетілмеген"

    await callback_query.answer("Тапсырысты алдыңыз!")

    # 1. Обновление карточки в группе
    order_label = "📦 ЖЕТКІЗУ (ДОСТАВКА)" if order_type == 'delivery' else "🚖 ТАПСЫРЫС"
    group_card_text = (
        f"✅ <b>{order_label} №{order_id} АЛЫНДЫ!</b>\n\n"
        f"📍 <b>Маршрут:</b> {esc(from_loc)} ➔ {esc(to_loc)}\n"
        f"💰 <b>Ақысы:</b> {esc(price)}\n"
        f"👤 <b>Жүргізуші:</b> {esc(driver_name)}"
    )
    try:
        await callback_query.message.edit_text(group_card_text, reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обновления группы: {e}")

    # 2. Карточка водителю в ЛС
    if order_type == 'delivery':
        driver_pm_text = (
            f"📦 <b>СІЗ ҚАБЫЛДАҒАН ЖЕТКІЗУ №{order_id}</b>\n\n"
            f"👤 <b>Клиент:</b> {esc(client_name)}\n"
            f"📦 <b>Зат / Посылка:</b> {esc(item_info)}\n"
            f"📍 <b>Қайдан алып кету:</b> {esc(from_loc)}\n"
            f"🏁 <b>Қайда жеткізу:</b> {esc(to_loc)}\n"
            f"💰 <b>Жеткізу ақысы:</b> {esc(price)}\n"
        )
    else:
        driver_pm_text = (
            f"🚨 <b>СІЗ ҚАБЫЛДАҒАН ТАПСЫРЫС №{order_id}</b>\n\n"
            f"👤 <b>Клиент:</b> {esc(client_name)}\n"
            f"📍 <b>Қайдан:</b> {esc(from_loc)}\n"
            f"🏁 <b>Қайда:</b> {esc(to_loc)}\n"
        )
        if order_type == 'intercity':
            driver_pm_text += f"📅 <b>Уақыты:</b> {esc(date_time)}\n👥 <b>Орын:</b> {esc(seats)}\n"
        driver_pm_text += f"💰 <b>Жол ақысы:</b> {esc(price)}\n"

    driver_pm_text += (
        f"💸 <b>Комиссия ұсталды:</b> {commission:.0f} ₸\n"
        f"💳 <b>Қалған баланс:</b> {new_balance:.0f} ₸\n"
        f"📞 <b>Телефоны:</b> <a href=\"tel:+{clean_client_phone}\">+{clean_client_phone}</a>"
    )

    wa_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 WhatsApp-пен жазу", url=f"https://wa.me/{clean_client_phone}")]
    ])
    driver_msg_id = None
    try:
        driver_msg = await bot.send_message(chat_id=driver.id, text=driver_pm_text, reply_markup=wa_kb, parse_mode="HTML")
        driver_msg_id = driver_msg.message_id
    except Exception as e:
        logging.error(f"Ошибка отправки водителю: {e}")

    # 3. Карточка клиенту в ЛС
    client_msg = (
        f"🚖 <b>№{order_id} тапсырысыңызды жүргізуші қабылдады!</b>\n\n"
        f"👤 <b>Жүргізуші:</b> {esc(driver_name)}\n"
        f"📞 <b>Телефоны:</b> <a href=\"tel:+{clean_driver_phone}\">+{clean_driver_phone}</a>\n"
        f"🚘 <b>Көлігі:</b> {esc(driver_car)}\n\n"
        f"Жүргізуші сізбен жақында хабарласады."
    )
    # Кнопка отмены переезжает на новое сообщение клиента (со старого убираем)
    if prev_client_msg_id:
        await safe_remove_buttons(client_id, prev_client_msg_id)
    new_client_msg_id = None
    try:
        sent_client = await bot.send_message(chat_id=client_id, text=client_msg,
                                             reply_markup=cancel_order_kb(order_id), parse_mode="HTML")
        new_client_msg_id = sent_client.message_id
    except Exception as e:
        logging.error(f"Ошибка отправки клиенту: {e}")

    await db(_update_order, order_id, group_text=group_card_text, driver_msg_id=driver_msg_id,
             driver_text=driver_pm_text, client_msg_id=new_client_msg_id)


# ======================= ОТМЕНА ЗАКАЗА КЛИЕНТОМ =======================
@dp.callback_query(F.data.startswith("cancelorder_"))
async def client_cancel_order(callback_query: types.CallbackQuery):
    order_id = int(callback_query.data.split("_")[1])
    res = await db(_cancel_order, order_id, callback_query.from_user.id)

    if res["status"] == "not_found":
        await callback_query.answer("❌ Тапсырыс табылмады!", show_alert=True)
        return
    if res["status"] == "already":
        texts = {
            "cancelled": "Тапсырыс бұрын жойылған.",
            "expired": f"Тапсырыс {ORDER_TIMEOUT_MINUTES} минуттан кейін автоматты түрде жабылған.",
        }
        await callback_query.answer(texts.get(res["order_status"], "Бұл тапсырысты жою мүмкін емес."), show_alert=True)
        await safe_remove_buttons(callback_query.message.chat.id, callback_query.message.message_id)
        return

    await callback_query.answer("Тапсырыс жойылды")

    # 1. Сообщение клиента
    try:
        await callback_query.message.edit_text(
            callback_query.message.html_text + "\n\n❌ <b>Сіз тапсырысты жойдыңыз.</b>",
            reply_markup=None, parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Не удалось обновить сообщение клиента: {e}")

    # 2. Карточка в группе
    if res["group_message_id"] and res["group_text"]:
        await safe_edit(
            res["group_chat_id"], res["group_message_id"],
            f"❌ <b>КЛИЕНТ ТАПСЫРЫСТЫ ЖОЙДЫ №{order_id}</b>\n\n" + res["group_text"],
        )

    # 3. Карточка и уведомление водителю (если заказ уже был принят)
    if res["was_accepted"] and res["driver_id"]:
        if res["driver_msg_id"] and res["driver_text"]:
            await safe_edit(
                res["driver_id"], res["driver_msg_id"],
                f"❌ <b>КЛИЕНТ ТАПСЫРЫСТЫ ЖОЙДЫ №{order_id}</b>\n\n" + res["driver_text"],
            )
        notice = f"❌ <b>Клиент №{order_id} тапсырысын жойды.</b>"
        if res["refunded"]:
            notice += (
                f"\n\n💸 Комиссия <b>{res['commission']:.0f} ₸</b> балансыңызға қайтарылды."
                f"\n💳 <b>Ағымдағы баланс:</b> {res['new_balance']:.0f} ₸"
            )
        elif res["commission"] > 0:
            notice += f"\n\nℹ️ Комиссия <b>{res['commission']:.0f} ₸</b> қайтарылмайды."
        await notify_driver(res["driver_id"], notice)


# ======================= ЗАПАСНЫЕ ОБРАБОТЧИКИ (подключаются последними) =======================
@fsm.message(StateFilter("*"))
async def unexpected_input(message: types.Message, state: FSMContext):
    """Пользователь в каком-то шаге, а прислал не то (стикер, фото, файл вместо текста)."""
    current = await state.get_state()
    if current is None:
        return
    if current == TopupState.waiting_for_receipt.state:
        await message.answer("📎 Чектің суретін (скриншот) немесе файлын жіберіңіз.", reply_markup=cancel_menu())
    elif current == ClientRegister.phone.state or current == DriverRegister.phone.state:
        await message.answer("📱 Телефон нөміріңізді жазыңыз немесе «📱 Номерді жіберу» батырмасын басыңыз.",
                             reply_markup=phone_request_menu())
    elif current == OrderDelivery.delivery_type.state:
        await message.answer("📦 Жеткізу түрін төмендегі батырмалардан таңдаңыз.", reply_markup=delivery_menu())
    else:
        await message.answer("✍️ Жауапты мәтінмен жазыңыз.", reply_markup=cancel_menu())


_last_error_notice = 0.0


@dp.errors()
async def on_error(event):
    """Глобальный перехват ошибок: пишем в лог, предупреждаем админа (не чаще раза в минуту) и пользователя."""
    global _last_error_notice
    logging.error("Необработанная ошибка в обработчике", exc_info=event.exception)

    if time.time() - _last_error_notice > 60:
        _last_error_notice = time.time()
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ Ошибка бота: {repr(event.exception)[:500]}")
        except Exception:
            pass

    try:
        update = event.update
        if update.message:
            await update.message.answer("❌ Қате шықты. Қайталап көріңіз немесе /start басыңыз.")
        elif update.callback_query:
            await update.callback_query.answer("❌ Қате шықты. Қайталап көріңіз.", show_alert=True)
    except Exception:
        pass
    return True


dp.include_router(fsm)


# ======================= АВТООТМЕНА НЕПРИНЯТЫХ ЗАКАЗОВ =======================
async def process_expired_order(order_id, user_id, group_chat_id, group_message_id, group_text, client_msg_id):
    if group_message_id and group_text:
        await safe_edit(
            group_chat_id, group_message_id,
            f"⌛ <b>ТАПСЫРЫС №{order_id} ЖОЙЫЛДЫ — {ORDER_TIMEOUT_MINUTES} минут ішінде ешкім қабылдамады</b>\n\n" + group_text,
        )
    if client_msg_id:
        await safe_remove_buttons(user_id, client_msg_id)
    if group_message_id:  # старые заказы (до обновления) закрываем молча, без сообщений клиентам
        try:
            await bot.send_message(
                user_id,
                f"⌛ <b>№{order_id} тапсырысыңыз жойылды.</b>\n\n"
                f"{ORDER_TIMEOUT_MINUTES} минут ішінде ешбір жүргізуші қабылдамады. Қажет болса, жаңа тапсырыс беріңіз.",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить клиента {user_id} об автоотмене: {e}")


async def expire_orders_loop():
    while True:
        try:
            for row in await db(_expire_orders):
                await process_expired_order(*row)
        except Exception as e:
            logging.error(f"Ошибка автоотмены заказов: {e}")
        await asyncio.sleep(30)


# ======================= ВЕБ-СЕРВЕР И ЗАПУСК =======================
async def handle_ping(request):
    return web.Response(text="Bot is live!")


async def main():
    await db(_init_db)

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await bot.delete_webhook(drop_pending_updates=True)
    expire_task = asyncio.create_task(expire_orders_loop())
    try:
        await dp.start_polling(bot)
    finally:
        expire_task.cancel()
        await runner.cleanup()
        pool.closeall()


if __name__ == '__main__':
    asyncio.run(main())
