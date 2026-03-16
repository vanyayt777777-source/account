import os
import json
import asyncio
import logging
import zipfile
import re
import shutil
import tempfile
import sqlite3
import requests
import hashlib
import hmac
import time
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered
import phonenumbers

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
CRYPTO_BOT_API = "549010:AAppnlCnLcg0vq9FR5CKDE8vpatHDV5FYvT"
CRYPTO_BOT_URL = "https://pay.crypt.bot/api"
ADMIN_ID = 7973988177

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
DATABASE_FILE = "shop.db"

# Создаем необходимые директории
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Класс для работы с Crypto Bot API
class CryptoBotAPI:
    def __init__(self, api_token):
        self.api_token = api_token
        self.headers = {
            "Crypto-Pay-API-Token": api_token,
            "Content-Type": "application/json"
        }
    
    async def create_invoice(self, amount, currency, description=""):
        """Создание счета на оплату"""
        url = f"{CRYPTO_BOT_URL}/createInvoice"
        
        payload = {
            "amount": str(amount),
            "currency_type": currency,
            "description": description,
            "paid_btn_name": "viewItem",
            "paid_btn_url": "https://t.me/vestaccountbot",
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 60  # 60 минут на оплату
        }
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    return {
                        "success": True,
                        "invoice_id": data["result"]["invoice_id"],
                        "pay_url": data["result"]["pay_url"],
                        "amount": data["result"]["amount"],
                        "currency": data["result"]["currency_type"],
                        "status": data["result"]["status"]
                    }
                else:
                    return {"success": False, "error": data.get("error", "Unknown error")}
            else:
                return {"success": False, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def check_invoice(self, invoice_id):
        """Проверка статуса счета"""
        url = f"{CRYPTO_BOT_URL}/getInvoices"
        
        payload = {
            "invoice_ids": [invoice_id]
        }
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok") and data.get("result") and data["result"].get("items"):
                    invoice = data["result"]["items"][0]
                    return {
                        "success": True,
                        "status": invoice["status"],
                        "paid_at": invoice.get("paid_at")
                    }
                else:
                    return {"success": False, "error": data.get("error", "Invoice not found")}
            else:
                return {"success": False, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_balance(self):
        """Получение баланса"""
        url = f"{CRYPTO_BOT_URL}/getBalance"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    return {"success": True, "balance": data["result"]}
                else:
                    return {"success": False, "error": data.get("error", "Unknown error")}
            else:
                return {"success": False, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"success": False, "error": str(e)}

# Инициализация Crypto Bot API
crypto_api = CryptoBotAPI(CRYPTO_BOT_API)

# Классы для работы с базой данных
class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()
    
    def init_db(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            
            # Таблица пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance REAL DEFAULT 0,
                    registered_date TEXT
                )
            ''')
            
            # Таблица аккаунтов для продажи
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS shop_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT,
                    country TEXT,
                    phone_number TEXT UNIQUE,
                    session_file TEXT,
                    price REAL,
                    added_date TEXT,
                    added_by INTEGER,
                    sold INTEGER DEFAULT 0,
                    sold_date TEXT,
                    sold_to INTEGER
                )
            ''')
            
            # Таблица покупок
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    account_id INTEGER,
                    phone_number TEXT,
                    category TEXT,
                    country TEXT,
                    price REAL,
                    payment_method TEXT,
                    purchase_date TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (account_id) REFERENCES shop_accounts (id)
                )
            ''')
            
            # Таблица для временных данных оплаты
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS crypto_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    account_id INTEGER,
                    amount REAL,
                    currency TEXT,
                    invoice_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_date TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (account_id) REFERENCES shop_accounts (id)
                )
            ''')
            
            conn.commit()
    
    # Работа с пользователями
    def get_user(self, user_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return cursor.fetchone()
    
    def create_user(self, user_id, username, first_name):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, registered_date) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
    
    def update_balance(self, user_id, amount):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()
    
    def get_balance(self, user_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0
    
    # Работа с аккаунтами магазина
    def add_shop_account(self, category, country, phone_number, session_file, price, added_by):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO shop_accounts (category, country, phone_number, session_file, price, added_date, added_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (category, country, phone_number, session_file, price, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), added_by)
            )
            conn.commit()
            return cursor.lastrowid
    
    def get_available_accounts(self, category=None):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            if category:
                cursor.execute("SELECT * FROM shop_accounts WHERE category = ? AND sold = 0 ORDER BY id", (category,))
            else:
                cursor.execute("SELECT * FROM shop_accounts WHERE sold = 0 ORDER BY id")
            return cursor.fetchall()
    
    def get_account_by_id(self, account_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM shop_accounts WHERE id = ?", (account_id,))
            return cursor.fetchone()
    
    def mark_as_sold(self, account_id, user_id, payment_method):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            # Получаем информацию об аккаунте
            cursor.execute("SELECT * FROM shop_accounts WHERE id = ?", (account_id,))
            account = cursor.fetchone()
            
            if account:
                # Отмечаем как проданный
                cursor.execute(
                    "UPDATE shop_accounts SET sold = 1, sold_date = ?, sold_to = ? WHERE id = ?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, account_id)
                )
                
                # Добавляем запись о покупке
                cursor.execute(
                    "INSERT INTO purchases (user_id, account_id, phone_number, category, country, price, payment_method, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, account_id, account[3], account[1], account[2], account[5], payment_method, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
                conn.commit()
                return True
            return False
    
    def get_user_purchases(self, user_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC", (user_id,))
            return cursor.fetchall()
    
    # Статистика
    def get_stats(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            
            # Всего пользователей
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            
            # Всего продаж
            cursor.execute("SELECT COUNT(*) FROM purchases")
            total_sales = cursor.fetchone()[0]
            
            # Общая выручка
            cursor.execute("SELECT SUM(price) FROM purchases")
            total_revenue = cursor.fetchone()[0] or 0
            
            # Доступные аккаунты по категориям
            cursor.execute("SELECT category, COUNT(*) FROM shop_accounts WHERE sold = 0 GROUP BY category")
            available = cursor.fetchall()
            
            return {
                "total_users": total_users,
                "total_sales": total_sales,
                "total_revenue": total_revenue,
                "available": available
            }
    
    # Crypto payments
    def create_crypto_payment(self, user_id, account_id, amount, currency, invoice_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO crypto_payments (user_id, account_id, amount, currency, invoice_id, created_date) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, account_id, amount, currency, invoice_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
            return cursor.lastrowid
    
    def get_payment_by_invoice(self, invoice_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM crypto_payments WHERE invoice_id = ?", (invoice_id,))
            return cursor.fetchone()
    
    def update_payment_status(self, invoice_id, status):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE crypto_payments SET status = ? WHERE invoice_id = ?", (status, invoice_id))
            conn.commit()

# Инициализация базы данных
db = Database(DATABASE_FILE)

# Класс для работы с Pyrogram сессиями
class SessionManager:
    @staticmethod
    async def process_session_file(file_path, file_name, temp_dir):
        """Обрабатывает загруженный session файл и возвращает информацию об аккаунте"""
        session_path = None
        session_name = None
        
        if file_name.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                session_files = [f for f in zip_ref.namelist() if f.endswith('.session')]
                if not session_files:
                    return None, "В архиве нет .session файла"
                
                zip_ref.extractall(temp_dir)
                
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        if file.endswith('.session'):
                            session_path = os.path.join(root, file)
                            session_name = os.path.splitext(file)[0]
                            break
                    if session_path:
                        break
        else:
            session_path = file_path
            session_name = os.path.splitext(file_name)[0]
        
        if not session_path or not session_name:
            return None, "Не удалось найти session файл"
        
        # Переименовываем если нужно
        session_dir = os.path.dirname(session_path)
        base_session_name = os.path.basename(session_name)
        new_session_path = os.path.join(session_dir, f"{base_session_name}.session")
        if session_path != new_session_path:
            shutil.move(session_path, new_session_path)
            session_path = new_session_path
            session_name = base_session_name
        
        return {
            'session_path': session_path,
            'session_name': session_name,
            'session_dir': session_dir
        }, None
    
    @staticmethod
    async def get_account_info(session_path, session_name, session_dir):
        """Получает информацию об аккаунте из session файла"""
        client = None
        try:
            client = Client(
                name=session_name,
                api_id=API_ID,
                api_hash=API_HASH,
                workdir=session_dir,
                in_memory=False
            )
            
            await client.start()
            me = await client.get_me()
            await client.stop()
            
            return {
                'phone_number': me.phone_number,
                'first_name': me.first_name,
                'last_name': me.last_name,
                'username': me.username
            }, None
        except Exception as e:
            if client:
                try:
                    await client.stop()
                except:
                    pass
            return None, str(e)

# Reply клавиатура (под полем ввода)
def get_main_keyboard():
    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="💎 Купить аккаунт")
    keyboard.button(text="👤 Профиль")
    keyboard.button(text="🛠 Наши софты")
    keyboard.adjust(2, 1)
    return keyboard.as_markup(resize_keyboard=True)

# Inline клавиатуры
class Keyboards:
    @staticmethod
    def buy_categories():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="👤 ФИЗ аккаунты", callback_data="cat_phys")
        keyboard.button(text="🕊 Аккаунты с отлегой", callback_data="cat_relax")
        keyboard.button(text="🔥 Прогретые", callback_data="cat_warmed")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def account_list(accounts, page=0, category=""):
        keyboard = InlineKeyboardBuilder()
        
        # Показываем по 5 аккаунтов на странице
        start_idx = page * 5
        end_idx = start_idx + 5
        page_accounts = accounts[start_idx:end_idx]
        
        for acc in page_accounts:
            # Форматируем номер для отображения
            phone = acc[3]
            masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
            btn_text = f"{masked_phone} | {acc[2]} | {acc[5]}₽"
            keyboard.button(text=btn_text, callback_data=f"view_acc_{acc[0]}")
        
        # Навигация
        nav_buttons = []
        if page > 0:
            nav_buttons.append(("◀️ Назад", f"page_{category}_{page-1}"))
        if end_idx < len(accounts):
            nav_buttons.append(("Вперед ▶️", f"page_{category}_{page+1}"))
        
        if nav_buttons:
            for btn_text, callback in nav_buttons:
                keyboard.button(text=btn_text, callback_data=callback)
        
        keyboard.button(text="◀️ В главное меню", callback_data="back_to_main")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def account_detail(account_id, price, balance_available=False):
        keyboard = InlineKeyboardBuilder()
        
        if balance_available:
            keyboard.button(text=f"💰 Оплатить {price}₽ (Баланс)", callback_data=f"pay_balance_{account_id}")
        
        keyboard.button(text="💎 Crypto Bot (USDT)", callback_data=f"pay_crypto_USDT_{account_id}")
        keyboard.button(text="💎 Crypto Bot (TON)", callback_data=f"pay_crypto_TON_{account_id}")
        keyboard.button(text="◀️ Назад к списку", callback_data=f"back_to_cat_prev")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def check_payment(invoice_id, account_id):
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="🔄 Проверить оплату", callback_data=f"check_payment_{invoice_id}_{account_id}")
        keyboard.button(text="◀️ Отмена", callback_data=f"back_to_cat_prev")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def after_purchase(account_id, phone_number):
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📨 Получить код", callback_data=f"get_code_{account_id}")
        keyboard.button(text="💎 Купить еще", callback_data="back_to_cat_prev")
        keyboard.adjust(1)
        
        # Создаем сообщение с номером
        text = (
            f"✅ <b>Покупка успешна!</b>\n\n"
            f"📱 <b>Номер аккаунта:</b>\n"
            f"<code>{phone_number}</code>\n\n"
            f"👇 Нажмите кнопку ниже, чтобы получить код подтверждения:"
        )
        return text, keyboard.as_markup()
    
    @staticmethod
    def profile_menu():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📋 Мои покупки", callback_data="my_purchases")
        keyboard.button(text="◀️ Назад", callback_data="back_to_main")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def softs_menu():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📢 Наш канал", url="https://t.me/VestSoftTG")
        keyboard.button(text="🤖 Наш комбайн", url="https://t.me/VestSoftBot")
        keyboard.button(text="🆘 Поддержка", url="https://t.me/VestSoftSupport")
        keyboard.button(text="◀️ Назад", callback_data="back_to_main")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def admin_menu():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📊 Статистика", callback_data="admin_stats")
        keyboard.button(text="📢 Рассылка", callback_data="admin_mailing")
        keyboard.button(text="➕ Выставить аккаунт", callback_data="admin_add_account")
        keyboard.button(text="💰 Изменить баланс", callback_data="admin_edit_balance")
        keyboard.button(text="◀️ Выход", callback_data="back_to_main")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def admin_categories():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="👤 ФИЗ аккаунты", callback_data="admin_cat_phys")
        keyboard.button(text="🕊 Аккаунты с отлегой", callback_data="admin_cat_relax")
        keyboard.button(text="🔥 Прогретые", callback_data="admin_cat_warmed")
        keyboard.button(text="◀️ Назад", callback_data="admin_menu")
        keyboard.adjust(1)
        return keyboard.as_markup()

# Состояния FSM
class AddAccountStates(StatesGroup):
    waiting_for_session = State()
    waiting_for_category = State()
    waiting_for_country = State()
    waiting_for_price = State()

class MailingStates(StatesGroup):
    waiting_for_message = State()

class EditBalanceStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()

# Хранилище для временных данных
user_states = {}

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    # Создаем пользователя в базе
    db.create_user(user_id, username, first_name)
    
    welcome_text = (
        f"🌟 <b>Добро пожаловать в VEST ACCOUNTS</b> 🌟\n\n"
        f"👋 Привет, {first_name}!\n\n"
        f"🤖 Я помогу тебе приобрести качественные Telegram аккаунты:\n"
        f"• 👤 ФИЗ аккаунты\n"
        f"• 🕊 Аккаунты с отлегой\n"
        f"• 🔥 Прогретые аккаунты\n\n"
        f"💳 <b>Доступные способы оплаты:</b>\n"
        f"• Внутренний баланс\n"
        f"• Crypto Bot (USDT/TON)\n\n"
        f"👇 <b>Выбери действие в меню ниже:</b>"
    )
    
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())

# Обработка Reply клавиатуры
@dp.message(F.text == "💎 Купить аккаунт")
async def handle_buy_button(message: Message):
    await message.answer(
        "📂 <b>Выберите категорию аккаунтов:</b>",
        parse_mode="HTML",
        reply_markup=Keyboards.buy_categories()
    )

@dp.message(F.text == "👤 Профиль")
async def handle_profile_button(message: Message):
    user_id = message.from_user.id
    user = db.get_user(user_id)
    
    if not user:
        await message.answer("❌ Ошибка загрузки профиля")
        return
    
    balance = user[3] if user else 0
    purchases = db.get_user_purchases(user_id)
    purchases_count = len(purchases)
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> @{message.from_user.username or 'отсутствует'}\n"
        f"💰 <b>Баланс:</b> {balance}₽\n"
        f"📦 <b>Куплено аккаунтов:</b> {purchases_count}\n"
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=Keyboards.profile_menu())

@dp.message(F.text == "🛠 Наши софты")
async def handle_softs_button(message: Message):
    text = (
        "🛠 <b>Наши продукты и сервисы</b>\n\n"
        "📢 <b>Канал с софтами:</b> @VestSoftTG\n"
        "   — Здесь публикуются все наши новые софты\n\n"
        "🤖 <b>Комбайн для аккаунтов:</b> @VestSoftBot\n"
        "   — Удобный инструмент для работы с аккаунтами\n\n"
        "🆘 <b>Поддержка:</b> @VestSoftSupport\n"
        "   — Помощь по любым вопросам\n\n"
        "👇 <b>Нажмите на кнопки ниже для перехода:</b>"
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=Keyboards.softs_menu())

# Главное меню inline
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "🌟 <b>Главное меню</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# Меню покупки
@dp.callback_query(F.data == "buy_menu")
async def buy_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "📂 <b>Выберите категорию аккаунтов:</b>",
        parse_mode="HTML",
        reply_markup=Keyboards.buy_categories()
    )
    await callback.answer()

# Показ списка аккаунтов по категориям
@dp.callback_query(F.data.startswith("cat_"))
async def show_category_accounts(callback: CallbackQuery):
    category_map = {
        "cat_phys": "ФИЗ аккаунты",
        "cat_relax": "Аккаунты с отлегой",
        "cat_warmed": "Прогретые"
    }
    
    category_key = callback.data
    category_name = category_map.get(category_key)
    
    if not category_name:
        await callback.answer("Ошибка категории")
        return
    
    # Сохраняем текущую категорию
    user_states[callback.from_user.id] = {"category": category_name}
    
    # Получаем доступные аккаунты
    accounts = db.get_available_accounts(category_name)
    
    if not accounts:
        await callback.message.edit_text(
            f"😕 <b>В категории '{category_name}' пока нет доступных аккаунтов</b>\n\n"
            f"Попробуйте позже или выберите другую категорию.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="buy_menu").as_markup()
        )
        await callback.answer()
        return
    
    # Показываем первую страницу
    text = f"📱 <b>{category_name}</b>\n\n<b>Доступные аккаунты:</b>\n"
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.account_list(accounts, 0, category_key)
    )
    await callback.answer()

# Пагинация
@dp.callback_query(F.data.startswith("page_"))
async def handle_pagination(callback: CallbackQuery):
    parts = callback.data.split("_")
    category_key = parts[1]
    page = int(parts[2])
    
    category_map = {
        "cat_phys": "ФИЗ аккаунты",
        "cat_relax": "Аккаунты с отлегой",
        "cat_warmed": "Прогретые"
    }
    
    category_name = category_map.get(f"cat_{category_key.split('_')[-1]}")
    accounts = db.get_available_accounts(category_name)
    
    text = f"📱 <b>{category_name}</b>\n\n<b>Доступные аккаунты:</b>\n"
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.account_list(accounts, page, f"cat_{category_key.split('_')[-1]}")
    )
    await callback.answer()

# Возврат к списку категории
@dp.callback_query(F.data == "back_to_cat_prev")
async def back_to_category(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in user_states and "category" in user_states[user_id]:
        category_name = user_states[user_id]["category"]
        category_key = {
            "ФИЗ аккаунты": "cat_phys",
            "Аккаунты с отлегой": "cat_relax",
            "Прогретые": "cat_warmed"
        }.get(category_name, "cat_phys")
        
        accounts = db.get_available_accounts(category_name)
        text = f"📱 <b>{category_name}</b>\n\n<b>Доступные аккаунты:</b>\n"
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.account_list(accounts, 0, category_key)
        )
    else:
        await callback.message.edit_text(
            "📂 <b>Выберите категорию аккаунтов:</b>",
            parse_mode="HTML",
            reply_markup=Keyboards.buy_categories()
        )
    await callback.answer()

# Просмотр деталей аккаунта
@dp.callback_query(F.data.startswith("view_acc_"))
async def view_account(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    
    # Получаем информацию об аккаунте
    account = db.get_account_by_id(account_id)
    if not account or account[7] == 1:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    # Получаем баланс пользователя
    user_id = callback.from_user.id
    balance = db.get_balance(user_id)
    
    # Форматируем номер для отображения
    phone = account[3]
    masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
    
    text = (
        f"📱 <b>Детали аккаунта</b>\n\n"
        f"📞 <b>Номер:</b> {masked_phone}\n"
        f"🌍 <b>Страна:</b> {account[2]}\n"
        f"💰 <b>Цена:</b> {account[5]}₽\n"
        f"💳 <b>Ваш баланс:</b> {balance}₽\n\n"
        f"<b>Выберите способ оплаты:</b>"
    )
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.account_detail(account_id, account[5], balance >= account[5])
    )
    await callback.answer()

# Профиль
@dp.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    
    if not user:
        await callback.answer("Ошибка загрузки профиля")
        return
    
    balance = user[3] if user else 0
    purchases = db.get_user_purchases(user_id)
    purchases_count = len(purchases)
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> @{callback.from_user.username or 'отсутствует'}\n"
        f"💰 <b>Баланс:</b> {balance}₽\n"
        f"📦 <b>Куплено аккаунтов:</b> {purchases_count}\n"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=Keyboards.profile_menu())
    await callback.answer()

@dp.callback_query(F.data == "my_purchases")
async def show_purchases(callback: CallbackQuery):
    user_id = callback.from_user.id
    purchases = db.get_user_purchases(user_id)
    
    if not purchases:
        await callback.message.edit_text(
            "📭 <b>У вас пока нет покупок</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="profile").as_markup()
        )
        await callback.answer()
        return
    
    text = "📋 <b>Мои покупки:</b>\n\n"
    for p in purchases[:10]:  # Показываем последние 10
        phone = p[3]
        masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
        text += f"• {masked_phone} | {p[5]} | {p[6]}₽\n"
        text += f"  📅 {p[8]}\n\n"
    
    if len(purchases) > 10:
        text += f"<i>И еще {len(purchases) - 10} покупок...</i>"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="profile")
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

# Наши софты
@dp.callback_query(F.data == "softs")
async def show_softs(callback: CallbackQuery):
    text = (
        "🛠 <b>Наши продукты и сервисы</b>\n\n"
        "📢 <b>Канал с софтами:</b> @VestSoftTG\n"
        "   — Здесь публикуются все наши новые софты\n\n"
        "🤖 <b>Комбайн для аккаунтов:</b> @VestSoftBot\n"
        "   — Удобный инструмент для работы с аккаунтами\n\n"
        "🆘 <b>Поддержка:</b> @VestSoftSupport\n"
        "   — Помощь по любым вопросам\n\n"
        "👇 <b>Нажмите на кнопки ниже для перехода:</b>"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=Keyboards.softs_menu())
    await callback.answer()

# Обработка оплаты с баланса
@dp.callback_query(F.data.startswith("pay_balance_"))
async def pay_with_balance(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    # Получаем информацию об аккаунте
    account = db.get_account_by_id(account_id)
    if not account or account[7] == 1:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    price = account[5]
    balance = db.get_balance(user_id)
    
    if balance < price:
        await callback.answer("❌ Недостаточно средств на балансе", show_alert=True)
        return
    
    # Списываем деньги
    db.update_balance(user_id, -price)
    
    # Отмечаем аккаунт как проданный
    db.mark_as_sold(account_id, user_id, "balance")
    
    # Получаем полный номер
    phone_number = account[3]
    
    # Отправляем аккаунт пользователю
    text, keyboard = Keyboards.after_purchase(account_id, phone_number)
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("✅ Оплата прошла успешно!", show_alert=True)

# Обработка оплаты через Crypto Bot
@dp.callback_query(F.data.startswith("pay_crypto_"))
async def pay_with_crypto(callback: CallbackQuery):
    parts = callback.data.split("_")
    currency = parts[2]
    account_id = int(parts[3])
    
    user_id = callback.from_user.id
    
    # Получаем информацию об аккаунте
    account = db.get_account_by_id(account_id)
    if not account or account[7] == 1:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    price_rub = account[5]
    
    # Конвертация в криптовалюту
    if currency == "USDT":
        crypto_amount = round(price_rub / 90, 2)
    else:  # TON
        crypto_amount = round(price_rub / 120, 2)
    
    # Минимальная сумма для Crypto Bot
    if crypto_amount < 0.1:
        crypto_amount = 0.1
    
    # Создаем описание
    description = f"Аккаунт {account[2]} | {account[1]}"
    
    # Создаем счет в Crypto Bot
    result = await crypto_api.create_invoice(crypto_amount, currency, description)
    
    if not result["success"]:
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания счета</b>\n\n{result.get('error', 'Неизвестная ошибка')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    invoice_id = result["invoice_id"]
    pay_url = result["pay_url"]
    
    # Сохраняем в базу
    db.create_crypto_payment(user_id, account_id, crypto_amount, currency, invoice_id)
    
    text = (
        f"💎 <b>Оплата через Crypto Bot</b>\n\n"
        f"💰 <b>Сумма к оплате:</b> {crypto_amount} {currency}\n"
        f"💵 <b>Курс:</b> 1 {currency} = {90 if currency == 'USDT' else 120}₽\n"
        f"📱 <b>Аккаунт:</b> {account[2]}\n\n"
        f"🔗 <b>Ссылка для оплаты:</b>\n"
        f"{pay_url}\n\n"
        f"⏳ <b>Счет действителен 60 минут</b>\n\n"
        f"👇 <b>После оплаты нажмите кнопку проверки:</b>"
    )
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.check_payment(invoice_id, account_id)
    )
    await callback.answer()

# Проверка оплаты Crypto Bot
@dp.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    invoice_id = parts[2]
    account_id = int(parts[3])
    user_id = callback.from_user.id
    
    # Проверяем статус через API
    result = await crypto_api.check_invoice(invoice_id)
    
    if not result["success"]:
        await callback.answer(f"❌ Ошибка проверки: {result.get('error', 'Неизвестная ошибка')}", show_alert=True)
        return
    
    if result["status"] == "paid":
        # Обновляем статус в базе
        db.update_payment_status(invoice_id, 'paid')
        
        # Получаем аккаунт
        account = db.get_account_by_id(account_id)
        if not account or account[7] == 1:
            await callback.message.edit_text(
                "❌ <b>Аккаунт уже продан</b>",
                parse_mode="HTML",
                reply_markup=Keyboards.main_menu()
            )
            await callback.answer()
            return
        
        # Отмечаем как проданный
        db.mark_as_sold(account_id, user_id, f"crypto_{invoice_id}")
        
        # Получаем полный номер
        phone_number = account[3]
        
        # Отправляем аккаунт пользователю
        text, keyboard = Keyboards.after_purchase(account_id, phone_number)
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer("✅ Оплата подтверждена!", show_alert=True)
    elif result["status"] == "pending":
        await callback.answer("⏳ Счет еще не оплачен", show_alert=True)
    else:
        await callback.answer(f"❌ Статус счета: {result['status']}", show_alert=True)

# Получение кода из аккаунта
@dp.callback_query(F.data.startswith("get_code_"))
async def get_code(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    
    # Получаем информацию об аккаунте
    account = db.get_account_by_id(account_id)
    if not account:
        await callback.message.edit_text(
            "❌ <b>Аккаунт не найден</b>",
            parse_mode="HTML",
            reply_markup=Keyboards.main_menu()
        )
        await callback.answer()
        return
    
    await callback.message.edit_text("🔄 <b>Получаю последний код...</b>", parse_mode="HTML")
    
    # Путь к session файлу
    session_path = os.path.join(SESSIONS_DIR, account[4])
    
    if not os.path.exists(session_path):
        await callback.message.edit_text(
            "❌ <b>Файл сессии не найден</b>",
            parse_mode="HTML",
            reply_markup=Keyboards.main_menu()
        )
        await callback.answer()
        return
    
    # Функция для извлечения 5-значного кода
    def extract_5digit_code(text: str) -> Optional[str]:
        pattern = r'\b(\d{5})\b'
        matches = re.findall(pattern, text)
        return matches[0] if matches else None
    
    client = None
    try:
        session_name = os.path.splitext(account[4])[0]
        
        client = Client(
            name=session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSIONS_DIR,
            in_memory=False
        )
        
        await client.start()
        
        # Получаем последние 20 диалогов
        found_code = None
        found_chat = None
        
        async for dialog in client.get_dialogs(limit=20):
            try:
                async for message in client.get_chat_history(dialog.chat.id, limit=10):
                    if message.text:
                        code = extract_5digit_code(message.text)
                        if code:
                            found_code = code
                            found_chat = dialog.chat
                            break
                if found_code:
                    break
            except:
                continue
        
        await client.stop()
        
        if found_code:
            chat_info = "неизвестный чат"
            if found_chat:
                if found_chat.username:
                    chat_info = f"@{found_chat.username}"
                elif found_chat.title:
                    chat_info = found_chat.title
            
            text = (
                f"✅ <b>Код найден!</b>\n\n"
                f"🔑 <b>Код:</b> <code>{found_code}</code>\n"
                f"💬 <b>Источник:</b> {chat_info}\n\n"
                f"📱 <b>Аккаунт:</b> {account[3]}\n\n"
                f"<i>Нажмите на код чтобы скопировать</i>"
            )
            
            keyboard = InlineKeyboardBuilder()
            keyboard.button(text="💎 Купить еще", callback_data="back_to_cat_prev")
            keyboard.adjust(1)
            
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
        else:
            text = (
                "❌ <b>Код не найден</b>\n\n"
                "В последних сообщениях аккаунта нет 5-значного кода.\n\n"
                "Попробуйте запросить код позже или отправьте новый код на этот аккаунт."
            )
            
            keyboard = InlineKeyboardBuilder()
            keyboard.button(text="🔄 Попробовать снова", callback_data=f"get_code_{account_id}")
            keyboard.button(text="💎 Купить еще", callback_data="back_to_cat_prev")
            keyboard.adjust(1)
            
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
            
    except Exception as e:
        await callback.message.edit_text(
            f"❌ <b>Ошибка:</b> {str(e)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        logger.error(f"Error getting code: {e}", exc_info=True)
    finally:
        if client:
            try:
                await client.stop()
            except:
                pass

# Админ панель
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        return
    
    await message.answer(
        "👨‍💼 <b>Админ панель</b>",
        parse_mode="HTML",
        reply_markup=Keyboards.admin_menu()
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    stats = db.get_stats()
    
    text = (
        f"📊 <b>Статистика магазина</b>\n\n"
        f"👥 <b>Всего пользователей:</b> {stats['total_users']}\n"
        f"💰 <b>Всего продаж:</b> {stats['total_sales']}\n"
        f"💵 <b>Общая выручка:</b> {stats['total_revenue']}₽\n\n"
        f"📦 <b>Доступные аккаунты:</b>\n"
    )
    
    for cat, count in stats['available']:
        text += f"  • {cat}: {count} шт.\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="admin_menu")
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "📢 <b>Отправьте сообщение для рассылки всем пользователям:</b>\n\n"
        "<i>Можно отправлять текст, фото, видео, документы</i>",
        parse_mode="HTML"
    )
    await state.set_state(MailingStates.waiting_for_message)
    await callback.answer()

@dp.message(MailingStates.waiting_for_message)
async def admin_mailing_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    # Получаем всех пользователей
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()
    
    sent = 0
    failed = 0
    
    status_msg = await message.answer("🔄 <b>Начинаю рассылку...</b>", parse_mode="HTML")
    
    for user in users:
        user_id = user[0]
        try:
            if message.text:
                await bot.send_message(user_id, message.text, parse_mode="HTML")
            elif message.photo:
                await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption, parse_mode="HTML")
            elif message.video:
                await bot.send_video(user_id, message.video.file_id, caption=message.caption, parse_mode="HTML")
            elif message.document:
                await bot.send_document(user_id, message.document.file_id, caption=message.caption, parse_mode="HTML")
            
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.error(f"Failed to send to {user_id}: {e}")
    
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📨 <b>Отправлено:</b> {sent}\n"
        f"❌ <b>Не удалось:</b> {failed}",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data == "admin_edit_balance")
async def admin_edit_balance_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "💰 <b>Введите ID пользователя для изменения баланса:</b>",
        parse_mode="HTML"
    )
    await state.set_state(EditBalanceStates.waiting_for_user_id)
    await callback.answer()

@dp.message(EditBalanceStates.waiting_for_user_id)
async def admin_edit_balance_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        user_id = int(message.text.strip())
        
        # Проверяем существует ли пользователь
        user = db.get_user(user_id)
        if not user:
            await message.answer("❌ <b>Пользователь не найден</b>", parse_mode="HTML")
            await state.clear()
            return
        
        await state.update_data(target_user_id=user_id)
        await message.answer(
            f"💰 <b>Текущий баланс пользователя:</b> {user[3]}₽\n\n"
            f"<b>Введите сумму для изменения</b> (можно с минусом):",
            parse_mode="HTML"
        )
        await state.set_state(EditBalanceStates.waiting_for_amount)
    except ValueError:
        await message.answer("❌ <b>Неверный формат ID. Введите число:</b>", parse_mode="HTML")

@dp.message(EditBalanceStates.waiting_for_amount)
async def admin_edit_balance_amount(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        amount = float(message.text.strip())
        data = await state.get_data()
        user_id = data['target_user_id']
        
        db.update_balance(user_id, amount)
        new_balance = db.get_balance(user_id)
        
        await message.answer(
            f"✅ <b>Баланс пользователя {user_id} изменен</b>\n"
            f"💰 <b>Новый баланс:</b> {new_balance}₽",
            parse_mode="HTML"
        )
        
        # Уведомляем пользователя
        try:
            if amount > 0:
                await bot.send_message(
                    user_id,
                    f"💰 <b>Вам начислено {amount}₽</b>\n"
                    f"💳 <b>Текущий баланс:</b> {new_balance}₽",
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    user_id,
                    f"💸 <b>С вашего баланса списано {abs(amount)}₽</b>\n"
                    f"💳 <b>Текущий баланс:</b> {new_balance}₽",
                    parse_mode="HTML"
                )
        except:
            pass
        
    except ValueError:
        await message.answer("❌ <b>Неверный формат суммы. Введите число:</b>", parse_mode="HTML")
        return
    
    await state.clear()

# Добавление аккаунта админом
@dp.callback_query(F.data == "admin_add_account")
async def admin_add_account_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "📂 <b>Выберите категорию для аккаунта:</b>",
        parse_mode="HTML",
        reply_markup=Keyboards.admin_categories()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_cat_"))
async def admin_add_account_category(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    category_map = {
        "admin_cat_phys": "ФИЗ аккаунты",
        "admin_cat_relax": "Аккаунты с отлегой",
        "admin_cat_warmed": "Прогретые"
    }
    
    category = category_map.get(callback.data)
    await state.update_data(category=category)
    
    await callback.message.edit_text(
        "🌍 <b>Введите страну аккаунта</b> (например: Россия, Украина, США):",
        parse_mode="HTML"
    )
    await state.set_state(AddAccountStates.waiting_for_country)
    await callback.answer()

@dp.message(AddAccountStates.waiting_for_country)
async def admin_add_account_country(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    country = message.text.strip()
    await state.update_data(country=country)
    
    await message.answer(
        "📤 <b>Отправьте session файл</b> в формате .zip или .session\n\n"
        "<i>Бот автоматически определит номер аккаунта.</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddAccountStates.waiting_for_session)

@dp.message(AddAccountStates.waiting_for_session, F.document)
async def admin_add_account_session(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    document = message.document
    file_name = document.file_name
    
    if not (file_name.endswith('.zip') or file_name.endswith('.session')):
        await message.answer("❌ <b>Пожалуйста, отправьте файл .zip или .session</b>", parse_mode="HTML")
        return
    
    status_msg = await message.answer("⏳ <b>Загружаю файл...</b>", parse_mode="HTML")
    
    temp_workdir = tempfile.mkdtemp(dir=TEMP_DIR)
    
    try:
        file_path = os.path.join(temp_workdir, file_name)
        await bot.download(document, file_path)
        
        # Обрабатываем session файл
        result, error = await SessionManager.process_session_file(file_path, file_name, temp_workdir)
        
        if error:
            await status_msg.edit_text(f"❌ <b>{error}</b>", parse_mode="HTML")
            return
        
        # Получаем информацию об аккаунте
        account_info, error = await SessionManager.get_account_info(
            result['session_path'],
            result['session_name'],
            result['session_dir']
        )
        
        if error:
            await status_msg.edit_text(f"❌ <b>Ошибка при входе:</b> {error}", parse_mode="HTML")
            return
        
        phone_number = account_info['phone_number']
        
        # Сохраняем session файл
        permanent_session_path = os.path.join(SESSIONS_DIR, f"{result['session_name']}.session")
        
        if os.path.exists(permanent_session_path):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_session_name = f"{result['session_name']}_{timestamp}"
            permanent_session_path = os.path.join(SESSIONS_DIR, f"{new_session_name}.session")
            result['session_name'] = new_session_name
        else:
            shutil.copy2(result['session_path'], permanent_session_path)
        
        await state.update_data(
            phone_number=phone_number,
            session_file=f"{result['session_name']}.session"
        )
        
        await status_msg.edit_text(
            f"✅ <b>Аккаунт загружен</b>\n"
            f"📱 <b>Номер:</b> <code>{phone_number}</code>\n\n"
            f"💰 <b>Введите цену в рублях:</b>",
            parse_mode="HTML"
        )
        await state.set_state(AddAccountStates.waiting_for_price)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Ошибка:</b> {str(e)}", parse_mode="HTML")
        logger.error(f"Error adding account: {e}", exc_info=True)
    finally:
        shutil.rmtree(temp_workdir, ignore_errors=True)

@dp.message(AddAccountStates.waiting_for_price)
async def admin_add_account_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        price = float(message.text.strip())
        if price <= 0:
            await message.answer("❌ <b>Цена должна быть положительным числом</b>", parse_mode="HTML")
            return
        
        data = await state.get_data()
        
        # Добавляем в базу
        db.add_shop_account(
            data['category'],
            data['country'],
            data['phone_number'],
            data['session_file'],
            price,
            message.from_user.id
        )
        
        await message.answer(
            f"✅ <b>Аккаунт успешно добавлен в продажу!</b>\n\n"
            f"📱 <b>Номер:</b> <code>{data['phone_number']}</code>\n"
            f"🌍 <b>Страна:</b> {data['country']}\n"
            f"📂 <b>Категория:</b> {data['category']}\n"
            f"💰 <b>Цена:</b> {price}₽",
            parse_mode="HTML"
        )
        
    except ValueError:
        await message.answer("❌ <b>Неверный формат цены. Введите число:</b>", parse_mode="HTML")
        return
    
    await state.clear()

# Запуск бота
async def main():
    print("=" * 50)
    print("🤖 Бот VEST ACCOUNTS запущен!")
    print(f"📁 Сессии хранятся в: {SESSIONS_DIR}")
    print(f"🗄 База данных: {DATABASE_FILE}")
    print(f"👑 Админ ID: {ADMIN_ID}")
    print("=" * 50)
    
    # Проверяем подключение к Crypto Bot
    balance = await crypto_api.get_balance()
    if balance["success"]:
        print("✅ Crypto Bot API подключен")
    else:
        print("⚠️ Ошибка подключения к Crypto Bot API")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
