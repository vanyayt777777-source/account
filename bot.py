import os
import json
import asyncio
import logging
import zipfile
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import shutil

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait, AuthKeyUnregistered
import phonenumbers

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Берем токен из переменных окружения
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Директории
SESSIONS_DIR = "sessions"
ACCOUNTS_DATA_FILE = "accounts.json"
TEMP_DIR = "temp"

# Создаем необходимые директории
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Класс для хранения информации об аккаунте
class Account:
    def __init__(self, phone_number: str, country: str, session_file: str, added_date: str, added_by: int):
        self.phone_number = phone_number
        self.country = country
        self.session_file = session_file
        self.added_date = added_date
        self.added_by = added_by
        self.last_code = None
        self.last_code_time = None

    def to_dict(self):
        return {
            "phone_number": self.phone_number,
            "country": self.country,
            "session_file": self.session_file,
            "added_date": self.added_date,
            "added_by": self.added_by,
            "last_code": self.last_code,
            "last_code_time": self.last_code_time
        }

    @staticmethod
    def from_dict(data):
        account = Account(
            data["phone_number"],
            data["country"],
            data["session_file"],
            data["added_date"],
            data.get("added_by", 0)
        )
        account.last_code = data.get("last_code")
        account.last_code_time = data.get("last_code_time")
        return account

# Загрузка и сохранение аккаунтов
def load_accounts() -> Dict[str, Account]:
    if os.path.exists(ACCOUNTS_DATA_FILE):
        with open(ACCOUNTS_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return {phone: Account.from_dict(acc_data) for phone, acc_data in data.items()}
    return {}

def save_accounts(accounts: Dict[str, Account]):
    data = {phone: acc.to_dict() for phone, acc in accounts.items()}
    with open(ACCOUNTS_DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

accounts_db = load_accounts()

# Функция для определения страны по номеру телефона
def get_country_from_phone(phone_number: str) -> str:
    try:
        parsed_number = phonenumbers.parse(phone_number, None)
        country_name = phonenumbers.country_name_for_number(parsed_number, "ru")
        return country_name or "Неизвестно"
    except:
        return "Неизвестно"

# Функция для извлечения 5-значного кода из текста
def extract_5digit_code(text: str) -> Optional[str]:
    # Ищем 5 цифр подряд
    pattern = r'\b(\d{5})\b'
    matches = re.findall(pattern, text)
    return matches[0] if matches else None

# Состояния для FSM
class AddAccountStates(StatesGroup):
    waiting_for_session = State()

# Команда старт
@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📱 Получить аккаунт", callback_data="get_account")
    keyboard.button(text="📋 Список аккаунтов", callback_data="list_accounts")
    keyboard.button(text="➕ Добавить аккаунт", callback_data="add_account")
    keyboard.adjust(2)
    
    await message.answer(
        "👋 Добро пожаловать в VEST ACCOUNTS!\n\n"
        "Здесь вы можете добавить свои аккаунты Telegram и получать коды подтверждения.\n\n"
        "➡️ Чтобы добавить аккаунт, нажмите '➕ Добавить аккаунт'\n"
        "➡️ Чтобы получить код, нажмите '📱 Получить аккаунт'",
        reply_markup=keyboard.as_markup()
    )

# Обработчик добавления аккаунта (доступно всем)
@dp.callback_query(F.data == "add_account")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📤 Отправьте session файл в формате .zip или .session\n\n"
        "Бот автоматически распакует его и определит номер и страну аккаунта.\n\n"
        "⚠️ Файл .session должен быть из Pyrogram (Telegram Desktop сессии не подходят)"
    )
    await state.set_state(AddAccountStates.waiting_for_session)
    await callback.answer()

# Обработчик получения session файла
@dp.message(AddAccountStates.waiting_for_session, F.document)
async def handle_session_file(message: Message, state: FSMContext):
    document = message.document
    file_name = document.file_name
    
    # Проверяем расширение
    if not (file_name.endswith('.zip') or file_name.endswith('.session')):
        await message.answer("❌ Пожалуйста, отправьте файл .zip или .session")
        return
    
    status_msg = await message.answer("⏳ Загружаю файл...")
    
    # Скачиваем файл
    file_path = os.path.join(TEMP_DIR, file_name)
    await bot.download(document, file_path)
    
    try:
        session_name = None
        session_path = None
        
        # Если это zip архив
        if file_name.endswith('.zip'):
            await status_msg.edit_text("📦 Распаковываю архив...")
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                # Ищем .session файл в архиве
                session_files = [f for f in zip_ref.namelist() if f.endswith('.session')]
                if not session_files:
                    await status_msg.edit_text("❌ В архиве нет .session файла")
                    os.remove(file_path)
                    return
                
                # Извлекаем первый .session файл
                session_file = session_files[0]
                session_name = os.path.splitext(os.path.basename(session_file))[0]
                zip_ref.extract(session_file, TEMP_DIR)
                session_path = os.path.join(TEMP_DIR, session_file)
        else:
            # Это .session файл
            session_name = os.path.splitext(file_name)[0]
            session_path = file_path
        
        await status_msg.edit_text(f"🔑 Вхожу в аккаунт {session_name}...")
        
        # Пытаемся войти в аккаунт
        async with Client(
            name=session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=TEMP_DIR,
            in_memory=True
        ) as app:
            try:
                me = await app.get_me()
                phone_number = me.phone_number
                country = get_country_from_phone(phone_number)
                
                await status_msg.edit_text(f"✅ Успешный вход!\n"
                                         f"📱 Номер: {phone_number}\n"
                                         f"🌍 Страна: {country}")
            except AttributeError:
                await status_msg.edit_text("❌ Не удалось получить информацию об аккаунте. Сессия может быть повреждена.")
                return
        
        # Копируем session файл в постоянное хранилище
        permanent_session_path = os.path.join(SESSIONS_DIR, f"{session_name}.session")
        shutil.copy2(session_path, permanent_session_path)
        
        # Проверяем, не существует ли уже такой номер
        if phone_number in accounts_db:
            await status_msg.edit_text(f"❌ Аккаунт с номером {phone_number} уже существует в базе")
            os.remove(permanent_session_path)
            return
        
        # Сохраняем информацию об аккаунте
        account = Account(
            phone_number=phone_number,
            country=country,
            session_file=f"{session_name}.session",
            added_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            added_by=message.from_user.id
        )
        
        accounts_db[phone_number] = account
        save_accounts(accounts_db)
        
        await status_msg.edit_text(
            f"✅ Аккаунт успешно добавлен!\n\n"
            f"📱 Номер: {phone_number}\n"
            f"🌍 Страна: {country}\n"
            f"👤 Добавил: @{message.from_user.username or 'нет username'}"
        )
        
    except AuthKeyUnregistered:
        await status_msg.edit_text("❌ Сессия недействительна или устарела")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
        logger.error(f"Error adding account: {e}", exc_info=True)
    finally:
        # Очищаем временные файлы
        if os.path.exists(file_path):
            os.remove(file_path)
        if 'session_path' in locals() and session_path and os.path.exists(session_path):
            os.remove(session_path)
    
    await state.clear()

# Обработчик неверного формата при добавлении аккаунта
@dp.message(AddAccountStates.waiting_for_session)
async def handle_invalid_session(message: Message):
    await message.answer("❌ Пожалуйста, отправьте файл .zip или .session")

# Обработчик получения аккаунта
@dp.callback_query(F.data == "get_account")
async def get_account_list(callback: CallbackQuery):
    if not accounts_db:
        await callback.message.edit_text(
            "😕 Пока нет доступных аккаунтов\n\n"
            "Вы можете добавить свой аккаунт, нажав '➕ Добавить аккаунт'"
        )
        await callback.answer()
        return
    
    keyboard = InlineKeyboardBuilder()
    for phone, account in accounts_db.items():
        # Показываем последние 4 цифры номера
        masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
        button_text = f"{masked_phone} | {account.country}"
        keyboard.button(text=button_text, callback_data=f"account_{phone}")
    
    keyboard.button(text="◀️ Назад", callback_data="back_to_main")
    keyboard.adjust(1)
    
    await callback.message.edit_text(
        "📱 Выберите аккаунт для получения кода:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработчик выбора конкретного аккаунта
@dp.callback_query(F.data.startswith("account_"))
async def show_account_options(callback: CallbackQuery):
    phone = callback.data.replace("account_", "")
    account = accounts_db.get(phone)
    
    if not account:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📨 Получить последний код", callback_data=f"getcode_{phone}")
    keyboard.button(text="◀️ Назад к списку", callback_data="get_account")
    keyboard.adjust(1)
    
    masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
    
    await callback.message.edit_text(
        f"📱 Аккаунт: {masked_phone}\n"
        f"🌍 Страна: {account.country}\n"
        f"📅 Добавлен: {account.added_date}\n\n"
        f"Последний полученный код: {account.last_code or 'нет'}",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработчик получения кода
@dp.callback_query(F.data.startswith("getcode_"))
async def get_code_from_account(callback: CallbackQuery):
    phone = callback.data.replace("getcode_", "")
    account = accounts_db.get(phone)
    
    if not account:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    
    status_msg = await callback.message.edit_text("🔄 Получаю последние сообщения...")
    
    try:
        # Создаем клиент для этого аккаунта
        session_path = os.path.join(SESSIONS_DIR, account.session_file)
        session_name = os.path.splitext(account.session_file)[0]
        
        async with Client(
            name=session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSIONS_DIR,
            in_memory=True
        ) as app:
            # Получаем последние 10 диалогов
            dialogs = []
            async for dialog in app.get_dialogs(limit=10):
                dialogs.append(dialog)
            
            found_code = None
            found_chat = None
            
            # Ищем 5-значный код в последних сообщениях
            for dialog in dialogs:
                try:
                    # Получаем последние 5 сообщений из чата
                    async for message in app.get_chat_history(dialog.chat.id, limit=5):
                        if message.text:
                            code = extract_5digit_code(message.text)
                            if code:
                                found_code = code
                                found_chat = dialog.chat
                                break
                    if found_code:
                        break
                except Exception as e:
                    logger.error(f"Error getting messages from {dialog.chat.id}: {e}")
                    continue
            
            if found_code:
                # Обновляем информацию о последнем коде
                account.last_code = found_code
                account.last_code_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_accounts(accounts_db)
                
                chat_info = f"@{found_chat.username}" if found_chat.username else f"чат {found_chat.id}"
                
                await status_msg.edit_text(
                    f"✅ Найден код!\n\n"
                    f"🔑 Код: <code>{found_code}</code>\n"
                    f"💬 Откуда: {chat_info}\n"
                    f"📱 Аккаунт: +{phone[-12:]}\n\n"
                    f"Код скопирован в буфер обмена (нажмите чтобы скопировать)",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    "❌ Не найден 5-значный код в последних сообщениях.\n\n"
                    "Убедитесь, что на аккаунт приходили сообщения с кодами."
                )
                
    except AuthKeyUnregistered:
        await status_msg.edit_text("❌ Сессия аккаунта недействительна. Добавьте аккаунт заново.")
        # Удаляем недействительный аккаунт
        if phone in accounts_db:
            del accounts_db[phone]
            save_accounts(accounts_db)
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при получении кода: {str(e)}")
        logger.error(f"Error getting code: {e}", exc_info=True)

# Обработчик списка аккаунтов
@dp.callback_query(F.data == "list_accounts")
async def list_all_accounts(callback: CallbackQuery):
    if not accounts_db:
        await callback.message.edit_text(
            "😕 Нет добавленных аккаунтов",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_main").as_markup()
        )
        await callback.answer()
        return
    
    text = "📋 Список всех аккаунтов:\n\n"
    for phone, account in accounts_db.items():
        masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
        text += f"• {masked_phone} | {account.country}\n"
        text += f"  📅 {account.added_date}\n"
        if account.last_code:
            text += f"  🔑 Последний код: {account.last_code}\n"
        text += "\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="back_to_main")
    
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    await callback.answer()

# Обработчик возврата в главное меню
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📱 Получить аккаунт", callback_data="get_account")
    keyboard.button(text="📋 Список аккаунтов", callback_data="list_accounts")
    keyboard.button(text="➕ Добавить аккаунт", callback_data="add_account")
    keyboard.adjust(2)
    
    await callback.message.edit_text(
        "👋 Добро пожаловать в VEST ACCOUNTS!\n\n"
        "Здесь вы можете добавить свои аккаунты Telegram и получать коды подтверждения.\n\n"
        "➡️ Чтобы добавить аккаунт, нажмите '➕ Добавить аккаунт'\n"
        "➡️ Чтобы получить код, нажмите '📱 Получить аккаунт'",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Запуск бота
async def main():
    print("🤖 Бот VEST ACCOUNTS запущен!")
    print(f"📁 Сессии хранятся в: {SESSIONS_DIR}")
    print(f"📊 Всего аккаунтов в базе: {len(accounts_db)}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
