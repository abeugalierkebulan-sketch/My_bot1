from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, any_state
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

class AddressSetup(StatesGroup):
    waiting_for_address = State()

router = Router()

def get_address_settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏡 Үйге (Дом)", callback_data="add_addr_🏡 Үйге")],
        [InlineKeyboardButton(text="💼 Жұмысқа (Работа)", callback_data="add_addr_💼 Жұмысқа")],
        [InlineKeyboardButton(text="🏫 Мектепке (Школа)", callback_data="add_addr_🏫 Мектепке")],
        [InlineKeyboardButton(text="🧸 Балабақшаға (Садик)", callback_data="add_addr_🧸 Балабақшаға")]
    ])

# 1. Выбор сохраненного адреса (работает из состояния OrderCity.to_loc)
@router.callback_query(F.data.startswith("set_dest_"))
async def process_quick_destination(callback_query: types.CallbackQuery, state: FSMContext):
    from main import OrderCity
    
    target_address = callback_query.data.replace("set_dest_", "")
    await state.update_data(to_loc=target_address)
    await state.set_state(OrderCity.price)
    
    await callback_query.answer()
    await callback_query.message.edit_text(
        f"🏁 <b>Қайда:</b> {target_address}\n\n💰 <b>Жол ақысын қанша ұсынасыз?</b>", 
        parse_mode="HTML"
    )

# 2. Нажатие на "⚙️ Мекенжайларды баптау" (работает в любом состоянии)
@router.callback_query(F.data == "manage_addresses", any_state)
async def process_manage_addresses(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(
        "⚙️ <b>Сүйікті мекенжайларды сақтау</b>\n\nҚай категорияға мекенжай қосқыңыз келеді?",
        reply_markup=get_address_settings_kb(),
        parse_mode="HTML"
    )

# 3. Выбор категории (работает в любом состоянии)
@router.callback_query(F.data.startswith("add_addr_"), any_state)
async def start_add_address(callback_query: types.CallbackQuery, state: FSMContext):
    from main import cancel_menu
    
    title = callback_query.data.replace("add_addr_", "")
    await state.update_data(target_title=title)
    await state.set_state(AddressSetup.waiting_for_address)
    
    await callback_query.answer()
    await callback_query.message.answer(
        f"✍️ <b>{title}</b> үшін мекенжайды жазыңыз (мысалы: <i>Абай 45</i>):",
        reply_markup=cancel_menu(),
        parse_mode="HTML"
    )

# 4. Сохранение введенного адреса в БД
@router.message(AddressSetup.waiting_for_address)
async def save_user_address(message: types.Message, state: FSMContext):
    from main import get_db_connection, main_menu
    
    data = await state.get_data()
    title = data.get("target_title", "Мекенжай")
    address_text = message.text.strip()
    user_id = message.from_user.id

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_addresses (user_id, title, address)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, title) DO UPDATE SET address = EXCLUDED.address
    ''', (user_id, title, address_text))
    conn.commit()
    cursor.close()
    conn.close()

    await state.clear()
    await message.answer(
        f"✅ <b>{title}</b> ({address_text}) сәтті сақталды!", 
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )
