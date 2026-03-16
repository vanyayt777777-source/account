import os
import json
import asyncio
import logging
import zipfile
import re
import shutil
import tempfile
import asyncpg
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
import aiohttp
from io import BytesIO

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

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден в переменных окружения")

API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
CRYPTO_BOT_API = "549010:AAppnlCnLcg0vq9FR5CKDE8vpatHDV5FYvT"
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
TEMP_DIR = "temp"
IMAGES_DIR = "images"

# Создаем необходимые директории
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# Класс для работы с базой данных PostgreSQL
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None
    
    async def connect(self):
        """Создание пула соединений с БД"""
        self.pool = await asyncpg.create_pool(self.dsn)
        await self.init_db()
    
    async def init_db(self):
        """Инициализация таблиц"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance DECIMAL(10,2) DEFAULT 0,
                    registered_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица для истории пополнений
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS balance_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    amount DECIMAL(10,2),
                    payment_method TEXT,
                    status TEXT DEFAULT 'completed',
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Таблица аккаунтов для продажи
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS shop_accounts (
                    id SERIAL PRIMARY KEY,
                    category TEXT,
                    country TEXT,
                    phone_number TEXT UNIQUE,
                    session_file TEXT,
                    price DECIMAL(10,2),
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    added_by BIGINT,
                    sold INTEGER DEFAULT 0,
                    sold_date TIMESTAMP,
                    sold_to BIGINT
                )
            ''')
            
            # Таблица покупок
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    account_id INTEGER,
                    phone_number TEXT,
                    category TEXT,
                    country TEXT,
                    price DECIMAL(10,2),
                    payment_method TEXT,
                    purchase_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (account_id) REFERENCES shop_accounts(id)
                )
            ''')
            
            # Таблица для временных данных оплаты
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS crypto_payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    account_id INTEGER,
                    amount DECIMAL(10,2),
                    currency TEXT,
                    invoice_id TEXT UNIQUE,
                    status TEXT DEFAULT 'pending',
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (account_id) REFERENCES shop_accounts(id)
                )
            ''')
            
            # Таблица для изображений
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS images (
                    id SERIAL PRIMARY KEY,
                    image_type TEXT UNIQUE,
                    file_id TEXT,
                    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
    
    # Работа с пользователями
    async def get_user(self, user_id: int) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    
    async def create_user(self, user_id: int, username: str, first_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name) 
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO NOTHING
            ''', user_id, username, first_name)
    
    async def update_balance(self, user_id: int, amount: float, payment_method: str = "admin"):
        """Обновление баланса с записью в историю"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('''
                    UPDATE users SET balance = balance + $1 WHERE user_id = $2
                ''', amount, user_id)
                
                await conn.execute('''
                    INSERT INTO balance_history (user_id, amount, payment_method) 
                    VALUES ($1, $2, $3)
                ''', user_id, amount, payment_method)
    
    async def get_balance(self, user_id: int) -> float:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)
            return float(result) if result else 0
    
    async def get_balance_history(self, user_id: int, limit: int = 10) -> List[asyncpg.Record]:
        """Получение истории пополнений пользователя"""
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT * FROM balance_history 
                WHERE user_id = $1 
                ORDER BY created_date DESC 
                LIMIT $2
            ''', user_id, limit)
    
    # Топ покупок
    async def get_top_buyers(self, days: int = 30, limit: int = 10) -> List[asyncpg.Record]:
        """Топ покупателей за указанный период"""
        async with self.pool.acquire() as conn:
            cutoff_date = datetime.now() - timedelta(days=days)
            return await conn.fetch('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.first_name,
                    COUNT(p.id) as purchases_count,
                    COALESCE(SUM(p.price), 0) as total_spent
                FROM users u
                LEFT JOIN purchases p ON u.user_id = p.user_id AND p.purchase_date >= $1
                GROUP BY u.user_id, u.username, u.first_name
                HAVING COUNT(p.id) > 0
                ORDER BY total_spent DESC
                LIMIT $2
            ''', cutoff_date, limit)
    
    async def get_top_accounts(self, limit: int = 10) -> List[asyncpg.Record]:
        """Топ самых покупаемых аккаунтов"""
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT 
                    country,
                    category,
                    COUNT(*) as purchase_count,
                    SUM(price) as total_revenue
                FROM purchases
                GROUP BY country, category
                ORDER BY purchase_count DESC
                LIMIT $1
            ''', limit)
    
    # Массовое добавление аккаунтов
    async def add_shop_accounts_bulk(self, accounts_data: List[Dict]) -> Tuple[int, List[str]]:
        """Массовое добавление аккаунтов"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                added = 0
                errors = []
                
                for data in accounts_data:
                    try:
                        await conn.execute('''
                            INSERT INTO shop_accounts 
                            (category, country, phone_number, session_file, price, added_by) 
                            VALUES ($1, $2, $3, $4, $5, $6)
                        ''', data['category'], data['country'], data['phone_number'], 
                            data['session_file'], data['price'], data['added_by'])
                        added += 1
                    except Exception as e:
                        errors.append(f"{data['phone_number']}: {str(e)}")
                
                return added, errors
    
    async def add_shop_account(self, category, country, phone_number, session_file, price, added_by):
        async with self.pool.acquire() as conn:
            return await conn.fetchval('''
                INSERT INTO shop_accounts 
                (category, country, phone_number, session_file, price, added_by) 
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            ''', category, country, phone_number, session_file, price, added_by)
    
    async def get_available_accounts(self, category: str = None) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            if category:
                return await conn.fetch('''
                    SELECT * FROM shop_accounts 
                    WHERE category = $1 AND sold = 0 
                    ORDER BY id
                ''', category)
            else:
                return await conn.fetch('''
                    SELECT * FROM shop_accounts 
                    WHERE sold = 0 
                    ORDER BY id
                ''')
    
    async def get_account_by_id(self, account_id: int) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow('''
                SELECT * FROM shop_accounts WHERE id = $1
            ''', account_id)
    
    async def mark_as_sold(self, account_id: int, user_id: int, payment_method: str) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                account = await conn.fetchrow('''
                    SELECT * FROM shop_accounts WHERE id = $1
                ''', account_id)
                
                if account and account['sold'] == 0:
                    await conn.execute('''
                        UPDATE shop_accounts 
                        SET sold = 1, sold_date = CURRENT_TIMESTAMP, sold_to = $1 
                        WHERE id = $2
                    ''', user_id, account_id)
                    
                    await conn.execute('''
                        INSERT INTO purchases 
                        (user_id, account_id, phone_number, category, country, price, payment_method) 
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ''', user_id, account_id, account['phone_number'], account['category'], 
                        account['country'], account['price'], payment_method)
                    
                    return True
            return False
    
    async def get_user_purchases(self, user_id: int) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT * FROM purchases 
                WHERE user_id = $1 
                ORDER BY purchase_date DESC
            ''', user_id)
    
    async def get_stats(self) -> Dict:
        async with self.pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_sales = await conn.fetchval("SELECT COUNT(*) FROM purchases")
            total_revenue = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM purchases")
            
            available = await conn.fetch('''
                SELECT category, COUNT(*) 
                FROM shop_accounts 
                WHERE sold = 0 
                GROUP BY category
            ''')
            
            today_sales = await conn.fetchval('''
                SELECT COUNT(*) FROM purchases 
                WHERE purchase_date >= CURRENT_DATE
            ''')
            
            today_revenue = await conn.fetchval('''
                SELECT COALESCE(SUM(price), 0) FROM purchases 
                WHERE purchase_date >= CURRENT_DATE
            ''')
            
            return {
                "total_users": total_users,
                "total_sales": total_sales,
                "total_revenue": float(total_revenue),
                "today_sales": today_sales,
                "today_revenue": float(today_revenue),
                "available": [(row['category'], row['count']) for row in available]
            }
    
    # Работа с изображениями
    async def save_image(self, image_type: str, file_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO images (image_type, file_id, updated_date) 
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (image_type) 
                DO UPDATE SET file_id = $2, updated_date = CURRENT_TIMESTAMP
            ''', image_type, file_id)
    
    async def get_image(self, image_type: str) -> Optional[str]:
        async with self.pool.acquire() as conn:
            return await conn.fetchval('''
                SELECT file_id FROM images WHERE image_type = $1
            ''', image_type)
    
    async def get_all_image_types(self) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT image_type FROM images ORDER BY image_type")
            return [row['image_type'] for row in rows]
    
    # Crypto payments
    async def create_crypto_payment(self, user_id: int, account_id: int, amount: float, currency: str, invoice_id: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchval('''
                INSERT INTO crypto_payments 
                (user_id, account_id, amount, currency, invoice_id) 
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            ''', user_id, account_id, amount, currency, invoice_id)
    
    async def get_payment_by_invoice(self, invoice_id: str) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow('''
                SELECT * FROM crypto_payments WHERE invoice_id = $1
            ''', invoice_id)
    
    async def update_payment_status(self, invoice_id: str, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE crypto_payments SET status = $1 WHERE invoice_id = $2
            ''', status, invoice_id)

# Инициализация базы данных
db = Database(DATABASE_URL)

# Класс для работы с Crypto Bot API
class CryptoBotAPI:
    def __init__(self, api_token):
        self.api_token = api_token
        self.headers = {
            "Crypto-Pay-API-Token": api_token,
            "Content-Type": "application/json"
        }
        self.base_url = "https://pay.crypt.bot/api"
    
    async def create_invoice(self, amount, currency, description=""):
        """Создание счета на оплату"""
        url = f"{self.base_url}/createInvoice"
        
        payload = {
            "asset": currency.upper(),
            "amount": str(amount),
            "description": description[:1024],
            "hidden_message": "Спасибо за покупку! Ваш аккаунт будет выдан после оплаты.",
            "paid_btn_name": "viewItem",
            "paid_btn_url": "https://t.me/vestaccountbot",
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 60
        }
        
        payload = {k: v for k, v in payload.items() if v is not None}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=payload, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("ok"):
                            return {
                                "success": True,
                                "invoice_id": data["result"]["invoice_id"],
                                "pay_url": data["result"]["pay_url"],
                                "amount": data["result"]["amount"],
                                "currency": data["result"]["asset"],
                                "status": data["result"]["status"]
                            }
                        else:
                            error_msg = data.get("error", {}).get("message", "Unknown error") if isinstance(data.get("error"), dict) else data.get("error", "Unknown error")
                            return {"success": False, "error": error_msg}
                    else:
                        error_text = await response.text()
                        logger.error(f"CryptoBot API HTTP {response.status}: {error_text}")
                        return {"success": False, "error": f"HTTP {response.status}"}
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def check_invoice(self, invoice_id):
        """Проверка статуса счета"""
        url = f"{self.base_url}/getInvoices"
        
        payload = {
            "invoice_ids": [invoice_id]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=payload, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("ok"):
                            items = data.get("result", {}).get("items", [])
                            if items:
                                invoice = items[0]
                                return {
                                    "success": True,
                                    "status": invoice["status"],
                                    "paid_at": invoice.get("paid_at")
                                }
                            else:
                                return {"success": False, "error": "Invoice not found"}
                        else:
                            error_msg = data.get("error", {}).get("message", "Unknown error") if isinstance(data.get("error"), dict) else data.get("error", "Unknown error")
                            return {"success": False, "error": error_msg}
                    else:
                        error_text = await response.text()
                        return {"success": False, "error": f"HTTP {response.status}"}
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_balance(self):
        """Получение баланса"""
        url = f"{self.base_url}/getBalance"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("ok"):
                            return {"success": True, "balance": data["result"]}
                        else:
                            error_msg = data.get("error", {}).get("message", "Unknown error") if isinstance(data.get("error"), dict) else data.get("error", "Unknown error")
                            return {"success": False, "error": error_msg}
                    else:
                        error_text = await response.text()
                        return {"success": False, "error": f"HTTP {response.status}"}
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"success": False, "error": str(e)}

# Инициализация Crypto Bot API
crypto_api = CryptoBotAPI(CRYPTO_BOT_API)

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
    
    @staticmethod
    async def process_multiple_sessions(zip_path: str, temp_dir: str) -> Tuple[List[Dict], List[str]]:
        """Обрабатывает zip архив с множеством session файлов"""
        successful = []
        errors = []
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            session_files = [f for f in zip_ref.namelist() if f.endswith('.session')]
            
            if not session_files:
                return [], ["В архиве нет .session файлов"]
            
            zip_ref.extractall(temp_dir)
            
            for session_file in session_files:
                try:
                    session_path = os.path.join(temp_dir, session_file)
                    session_name = os.path.splitext(os.path.basename(session_file))[0]
                    session_dir = os.path.dirname(session_path)
                    
                    account_info, error = await SessionManager.get_account_info(
                        session_path, session_name, session_dir
                    )
                    
                    if error:
                        errors.append(f"{session_file}: {error}")
                        continue
                    
                    permanent_session_path = os.path.join(SESSIONS_DIR, f"{session_name}.session")
                    
                    if os.path.exists(permanent_session_path):
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        new_session_name = f"{session_name}_{timestamp}"
                        permanent_session_path = os.path.join(SESSIONS_DIR, f"{new_session_name}.session")
                        session_name = new_session_name
                    else:
                        shutil.copy2(session_path, permanent_session_path)
                    
                    successful.append({
                        'session_name': session_name,
                        'session_file': f"{session_name}.session",
                        'phone_number': account_info['phone_number']
                    })
                    
                except Exception as e:
                    errors.append(f"{session_file}: {str(e)}")
        
        return successful, errors

# Reply клавиатура (под полем ввода)
def get_main_keyboard():
    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="💎 Купить аккаунт")
    keyboard.button(text="📱 Номера под смену")
    keyboard.button(text="👤 Профиль")
    keyboard.button(text="🛠 Наши софты")
    keyboard.adjust(2, 2)
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
        
        start_idx = page * 5
        end_idx = start_idx + 5
        page_accounts = accounts[start_idx:end_idx]
        
        for acc in page_accounts:
            phone = acc['phone_number']
            masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
            btn_text = f"{masked_phone} | {acc['country']} | {acc['price']}₽"
            keyboard.button(text=btn_text, callback_data=f"view_acc_{acc['id']}")
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(("◀️ Назад", f"page_{category}_{page-1}"))
        if end_idx < len(accounts):
            nav_buttons.append(("Вперед ▶️", f"page_{category}_{page+1}"))
        
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
        keyboard.button(text="💰 Пополнить баланс", callback_data="deposit")
        keyboard.button(text="📋 Мои покупки", callback_data="my_purchases")
        keyboard.button(text="📊 История пополнений", callback_data="balance_history")
        keyboard.button(text="🏆 Топ покупателей", callback_data="top_buyers")
        keyboard.button(text="◀️ Назад", callback_data="back_to_main")
        keyboard.adjust(1)
        return keyboard.as_markup()
    
    @staticmethod
    def deposit_menu():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="100 ₽", callback_data="deposit_100")
        keyboard.button(text="500 ₽", callback_data="deposit_500")
        keyboard.button(text="1000 ₽", callback_data="deposit_1000")
        keyboard.button(text="Другая сумма", callback_data="deposit_custom")
        keyboard.button(text="◀️ Назад", callback_data="profile")
        keyboard.adjust(2)
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
        keyboard.button(text="📦 Массовое добавление", callback_data="admin_bulk_add")
        keyboard.button(text="💰 Изменить баланс", callback_data="admin_edit_balance")
        keyboard.button(text="🖼 Изображения", callback_data="admin_images")
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
    
    @staticmethod
    def admin_image_categories():
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="🏠 Приветствие", callback_data="img_welcome")
        keyboard.button(text="👤 Профиль", callback_data="img_profile")
        keyboard.button(text="📱 Выбор аккаунта", callback_data="img_account_select")
        keyboard.button(text="✅ Выдача аккаунта", callback_data="img_account_give")
        keyboard.button(text="📋 Список изображений", callback_data="img_list")
        keyboard.button(text="◀️ Назад", callback_data="admin_menu")
        keyboard.adjust(1)
        return keyboard.as_markup()

# Состояния FSM
class AddAccountStates(StatesGroup):
    waiting_for_session = State()
    waiting_for_category = State()
    waiting_for_country = State()
    waiting_for_price = State()

class BulkAddStates(StatesGroup):
    waiting_for_zip = State()
    waiting_for_category = State()
    waiting_for_country = State()
    waiting_for_price = State()
    waiting_for_confirmation = State()

class MailingStates(StatesGroup):
    waiting_for_message = State()

class EditBalanceStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()

class DepositStates(StatesGroup):
    waiting_for_amount = State()

class ImageStates(StatesGroup):
    waiting_for_image = State()

# Хранилище для временных данных
user_states = {}

# Функция для отправки сообщения с изображением (исправленная версия)
async def send_message_with_image(chat_id: int, text: str, image_type: str, reply_markup=None, parse_mode="HTML"):
    """Отправляет сообщение с изображением, если оно есть в БД"""
    file_id = await db.get_image(image_type)
    
    if file_id:
        try:
            # Отправляем фото с подписью
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            return True
        except Exception as e:
            logger.error(f"Error sending image {image_type}: {e}")
            # Если фото не отправилось, отправляем просто текст
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            return False
    else:
        # Если изображения нет, отправляем только текст
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
        return False

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    # Создаем пользователя в базе
    await db.create_user(user_id, username, first_name)
    
    welcome_text = (
        f"🌟 <b>Добро пожаловать в VEST ACCOUNTS</b> 🌟\n\n"
        f"👋 Привет, {first_name}!\n\n"
        f"🤖 Я помогу тебе приобрести качественные Telegram аккаунты:\n"
        f"• 👤 ФИЗ аккаунты\n"
        f"• 🕊 Аккаунты с отлегой\n"
        f"• 🔥 Прогретые аккаунты\n"
        f"• 📱 Номера под смену\n\n"
        f"💳 <b>Доступные способы оплаты:</b>\n"
        f"• Внутренний баланс\n"
        f"• Crypto Bot (USDT/TON)\n\n"
        f"👇 <b>Выбери действие в меню ниже:</b>"
    )
    
    await send_message_with_image(
        message.chat.id,
        welcome_text,
        "img_welcome",
        get_main_keyboard()
    )

# Обработка Reply клавиатуры
@dp.message(F.text == "💎 Купить аккаунт")
async def handle_buy_button(message: Message):
    text = "📂 <b>Выберите категорию аккаунтов:</b>"
    await send_message_with_image(
        message.chat.id,
        text,
        "img_account_select",
        Keyboards.buy_categories()
    )

@dp.message(F.text == "📱 Номера под смену")
async def handle_numbers_button(message: Message):
    text = (
        "🔄 <b>Номера под смену</b>\n\n"
        "🚧 Этот раздел находится в разработке.\n"
        "Следите за обновлениями в нашем канале: @VestSoftTG"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "👤 Профиль")
async def handle_profile_button(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    
    if not user:
        await message.answer("❌ Ошибка загрузки профиля")
        return
    
    balance = user['balance'] if user else 0
    purchases = await db.get_user_purchases(user_id)
    purchases_count = len(purchases)
    
    # Получаем топ покупателей
    top_buyers = await db.get_top_buyers(limit=5)
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> @{message.from_user.username or 'отсутствует'}\n"
        f"💰 <b>Баланс:</b> {balance}₽\n"
        f"📦 <b>Куплено аккаунтов:</b> {purchases_count}\n\n"
    )
    
    if top_buyers:
        text += "🏆 <b>Топ покупателей месяца:</b>\n"
        for i, buyer in enumerate(top_buyers[:3], 1):
            name = buyer['first_name'] or f"User{buyer['user_id']}"
            text += f"{i}. {name} — {buyer['purchases_count']} акк. ({buyer['total_spent']}₽)\n"
    
    # Отправляем с изображением профиля
    await send_message_with_image(
        message.chat.id,
        text,
        "img_profile",
        Keyboards.profile_menu()
    )

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
    text = "📂 <b>Выберите категорию аккаунтов:</b>"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=Keyboards.buy_categories())
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
    accounts = await db.get_available_accounts(category_name)
    
    if not accounts:
        await callback.message.edit_text(
            f"😕 <b>В категории '{category_name}' пока нет доступных аккаунтов</b>\n\n"
            f"Попробуйте позже или выберите другую категорию.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="buy_menu").as_markup()
        )
        await callback.answer()
        return
    
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
    accounts = await db.get_available_accounts(category_name)
    
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
        
        accounts = await db.get_available_accounts(category_name)
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
    
    account = await db.get_account_by_id(account_id)
    if not account or account['sold'] == 1:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    user_id = callback.from_user.id
    balance = await db.get_balance(user_id)
    
    phone = account['phone_number']
    masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
    
    text = (
        f"📱 <b>Детали аккаунта</b>\n\n"
        f"📞 <b>Номер:</b> {masked_phone}\n"
        f"🌍 <b>Страна:</b> {account['country']}\n"
        f"💰 <b>Цена:</b> {account['price']}₽\n"
        f"💳 <b>Ваш баланс:</b> {balance}₽\n\n"
        f"<b>Выберите способ оплаты:</b>"
    )
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.account_detail(account_id, account['price'], balance >= account['price'])
    )
    await callback.answer()

# Профиль inline
@dp.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    
    if not user:
        await callback.answer("Ошибка загрузки профиля")
        return
    
    balance = user['balance'] if user else 0
    purchases = await db.get_user_purchases(user_id)
    purchases_count = len(purchases)
    
    # Получаем топ покупателей
    top_buyers = await db.get_top_buyers(limit=5)
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> @{callback.from_user.username or 'отсутствует'}\n"
        f"💰 <b>Баланс:</b> {balance}₽\n"
        f"📦 <b>Куплено аккаунтов:</b> {purchases_count}\n\n"
    )
    
    if top_buyers:
        text += "🏆 <b>Топ покупателей месяца:</b>\n"
        for i, buyer in enumerate(top_buyers[:3], 1):
            name = buyer['first_name'] or f"User{buyer['user_id']}"
            text += f"{i}. {name} — {buyer['purchases_count']} акк. ({buyer['total_spent']}₽)\n"
    
    await callback.message.delete()
    await send_message_with_image(
        callback.message.chat.id,
        text,
        "img_profile",
        Keyboards.profile_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "my_purchases")
async def show_purchases(callback: CallbackQuery):
    user_id = callback.from_user.id
    purchases = await db.get_user_purchases(user_id)
    
    if not purchases:
        await callback.message.edit_text(
            "📭 <b>У вас пока нет покупок</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="profile").as_markup()
        )
        await callback.answer()
        return
    
    text = "📋 <b>Мои покупки:</b>\n\n"
    total_spent = 0
    for p in purchases[:10]:
        phone = p['phone_number']
        masked_phone = f"+{phone[-12:-8]}****{phone[-4:]}" if len(phone) > 4 else phone
        text += f"• {masked_phone} | {p['country']} | {p['price']}₽\n"
        text += f"  📅 {p['purchase_date'].strftime('%Y-%m-%d %H:%M')}\n\n"
        total_spent += p['price']
    
    text += f"<b>Всего потрачено:</b> {total_spent}₽"
    
    if len(purchases) > 10:
        text += f"\n\n<i>И еще {len(purchases) - 10} покупок...</i>"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="profile")
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "balance_history")
async def show_balance_history(callback: CallbackQuery):
    user_id = callback.from_user.id
    history = await db.get_balance_history(user_id)
    
    if not history:
        await callback.message.edit_text(
            "📭 <b>История пополнений пуста</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="profile").as_markup()
        )
        await callback.answer()
        return
    
    text = "💰 <b>История пополнений:</b>\n\n"
    for h in history:
        sign = "+" if h['amount'] > 0 else ""
        text += f"{sign}{h['amount']}₽ | {h['payment_method']}\n"
        text += f"  📅 {h['created_date'].strftime('%Y-%m-%d %H:%M')}\n\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="profile")
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "top_buyers")
async def show_top_buyers(callback: CallbackQuery):
    top_buyers = await db.get_top_buyers(days=30, limit=10)
    
    if not top_buyers:
        await callback.message.edit_text(
            "📭 <b>Пока нет данных о покупках</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="profile").as_markup()
        )
        await callback.answer()
        return
    
    text = "🏆 <b>Топ 10 покупателей месяца</b>\n\n"
    for i, buyer in enumerate(top_buyers, 1):
        name = buyer['first_name'] or f"User{buyer['user_id']}"
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        text += f"{medal} {name}\n"
        text += f"   📦 {buyer['purchases_count']} аккаунтов\n"
        text += f"   💰 {buyer['total_spent']}₽\n\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="profile")
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

# Пополнение баланса
@dp.callback_query(F.data == "deposit")
async def deposit_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n\n"
        "Выберите сумму пополнения:",
        parse_mode="HTML",
        reply_markup=Keyboards.deposit_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("deposit_"))
async def deposit_amount(callback: CallbackQuery, state: FSMContext):
    if callback.data == "deposit_custom":
        await callback.message.edit_text(
            "💰 <b>Введите сумму пополнения</b> (от 10 до 100000 ₽):",
            parse_mode="HTML"
        )
        await state.set_state(DepositStates.waiting_for_amount)
        await callback.answer()
        return
    
    amount = int(callback.data.split("_")[1])
    await process_deposit(callback.message, callback.from_user.id, amount, callback)

async def process_deposit(message, user_id, amount, callback=None):
    """Обработка пополнения баланса через Crypto Bot"""
    
    # Минимальная сумма 10₽
    if amount < 10:
        text = "❌ <b>Минимальная сумма пополнения: 10₽</b>"
        if callback:
            await callback.message.edit_text(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
        return
    
    # Конвертация в USDT (для примера)
    usdt_amount = round(amount / 90, 2)
    if usdt_amount < 0.1:
        usdt_amount = 0.1
    
    if callback:
        await callback.message.edit_text(
            f"⏳ <b>Создаю счет для пополнения...</b>\n\n"
            f"💰 Сумма: {amount}₽ ({usdt_amount} USDT)",
            parse_mode="HTML"
        )
    
    description = f"Пополнение баланса на {amount}₽"
    result = await crypto_api.create_invoice(usdt_amount, "USDT", description)
    
    if not result["success"]:
        error_text = result.get('error', 'Неизвестная ошибка')
        text = f"❌ <b>Ошибка создания счета</b>\n\n{error_text}"
        if callback:
            await callback.message.edit_text(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
        return
    
    invoice_id = result["invoice_id"]
    pay_url = result["pay_url"]
    
    # Сохраняем в базу как временную оплату (без account_id)
    await db.create_crypto_payment(user_id, 0, usdt_amount, "USDT", invoice_id)
    
    text = (
        f"💎 <b>Пополнение баланса</b>\n\n"
        f"💰 <b>Сумма:</b> {amount}₽\n"
        f"💵 <b>К оплате:</b> {usdt_amount} USDT\n\n"
        f"🔗 <b>Ссылка для оплаты:</b>\n"
        f"{pay_url}\n\n"
        f"⏳ <b>Счет действителен 60 минут</b>\n\n"
        f"👇 <b>После оплаты нажмите кнопку проверки:</b>"
    )
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔄 Проверить оплату", callback_data=f"check_deposit_{invoice_id}_{amount}")
    keyboard.button(text="◀️ Отмена", callback_data="profile")
    
    if callback:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard.as_markup())

@dp.callback_query(F.data.startswith("check_deposit_"))
async def check_deposit(callback: CallbackQuery):
    parts = callback.data.split("_")
    invoice_id = parts[2]
    amount = float(parts[3])
    user_id = callback.from_user.id
    
    result = await crypto_api.check_invoice(invoice_id)
    
    if not result["success"]:
        await callback.answer(f"❌ Ошибка проверки: {result.get('error', 'Неизвестная ошибка')}", show_alert=True)
        return
    
    if result["status"] == "paid":
        # Обновляем статус
        await db.update_payment_status(invoice_id, 'paid')
        
        # Начисляем баланс
        await db.update_balance(user_id, amount, "crypto_bot")
        
        new_balance = await db.get_balance(user_id)
        
        await callback.message.edit_text(
            f"✅ <b>Баланс успешно пополнен!</b>\n\n"
            f"💰 <b>Сумма:</b> {amount}₽\n"
            f"💳 <b>Текущий баланс:</b> {new_balance}₽",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="👤 В профиль", callback_data="profile").as_markup()
        )
        await callback.answer("✅ Оплата подтверждена!", show_alert=True)
    elif result["status"] == "pending":
        await callback.answer("⏳ Счет еще не оплачен", show_alert=True)
    else:
        await callback.answer(f"❌ Статус счета: {result['status']}", show_alert=True)

@dp.message(DepositStates.waiting_for_amount)
async def process_custom_deposit(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount < 10 or amount > 100000:
            await message.answer("❌ <b>Сумма должна быть от 10 до 100000 ₽</b>", parse_mode="HTML")
            return
        
        await process_deposit(message, message.from_user.id, amount)
        await state.clear()
        
    except ValueError:
        await message.answer("❌ <b>Неверный формат суммы. Введите число:</b>", parse_mode="HTML")

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
    
    account = await db.get_account_by_id(account_id)
    if not account or account['sold'] == 1:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    price = account['price']
    balance = await db.get_balance(user_id)
    
    if balance < price:
        await callback.answer("❌ Недостаточно средств на балансе", show_alert=True)
        return
    
    # Списываем деньги
    await db.update_balance(user_id, -price, "purchase")
    
    # Отмечаем аккаунт как проданный
    await db.mark_as_sold(account_id, user_id, "balance")
    
    phone_number = account['phone_number']
    text, keyboard = Keyboards.after_purchase(account_id, phone_number)
    
    await callback.message.delete()
    await send_message_with_image(
        callback.message.chat.id,
        text,
        "img_account_give",
        keyboard
    )
    await callback.answer("✅ Оплата прошла успешно!", show_alert=True)

# Обработка оплаты через Crypto Bot
@dp.callback_query(F.data.startswith("pay_crypto_"))
async def pay_with_crypto(callback: CallbackQuery):
    parts = callback.data.split("_")
    currency = parts[2]
    account_id = int(parts[3])
    
    user_id = callback.from_user.id
    
    account = await db.get_account_by_id(account_id)
    if not account or account['sold'] == 1:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_cat_prev").as_markup()
        )
        await callback.answer()
        return
    
    price_rub = account['price']
    
    if currency == "USDT":
        crypto_amount = round(price_rub / 90, 2)
        if crypto_amount < 0.1:
            crypto_amount = 0.1
    else:
        crypto_amount = round(price_rub / 120, 2)
        if crypto_amount < 0.1:
            crypto_amount = 0.1
    
    description = f"Аккаунт {account['country']} | {account['category']}"
    
    await callback.message.edit_text(
        f"⏳ <b>Создаю счет для оплаты...</b>\n\n"
        f"💰 Сумма: {crypto_amount} {currency}",
        parse_mode="HTML"
    )
    
    result = await crypto_api.create_invoice(crypto_amount, currency, description)
    
    if not result["success"]:
        error_text = result.get('error', 'Неизвестная ошибка')
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания счета</b>\n\n{error_text}\n\n"
            f"Попробуйте позже или выберите другой способ оплаты.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data=f"view_acc_{account_id}").as_markup()
        )
        await callback.answer()
        return
    
    invoice_id = result["invoice_id"]
    pay_url = result["pay_url"]
    
    await db.create_crypto_payment(user_id, account_id, crypto_amount, currency, invoice_id)
    
    text = (
        f"💎 <b>Оплата через Crypto Bot</b>\n\n"
        f"💰 <b>Сумма к оплате:</b> {crypto_amount} {currency}\n"
        f"💵 <b>Курс:</b> 1 {currency} = {90 if currency == 'USDT' else 120}₽\n"
        f"📱 <b>Аккаунт:</b> {account['country']}\n\n"
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
    
    result = await crypto_api.check_invoice(invoice_id)
    
    if not result["success"]:
        await callback.answer(f"❌ Ошибка проверки: {result.get('error', 'Неизвестная ошибка')}", show_alert=True)
        return
    
    if result["status"] == "paid":
        await db.update_payment_status(invoice_id, 'paid')
        
        account = await db.get_account_by_id(account_id)
        if not account or account['sold'] == 1:
            await callback.message.edit_text(
                "❌ <b>Аккаунт уже продан</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="back_to_main").as_markup()
            )
            await callback.answer()
            return
        
        await db.mark_as_sold(account_id, user_id, f"crypto_{invoice_id}")
        
        phone_number = account['phone_number']
        text, keyboard = Keyboards.after_purchase(account_id, phone_number)
        
        await callback.message.delete()
        await send_message_with_image(
            callback.message.chat.id,
            text,
            "img_account_give",
            keyboard
        )
        await callback.answer("✅ Оплата подтверждена!", show_alert=True)
    elif result["status"] == "pending":
        await callback.answer("⏳ Счет еще не оплачен", show_alert=True)
    else:
        await callback.answer(f"❌ Статус счета: {result['status']}", show_alert=True)

# Получение кода из аккаунта
@dp.callback_query(F.data.startswith("get_code_"))
async def get_code(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    
    account = await db.get_account_by_id(account_id)
    if not account:
        await callback.message.edit_text(
            "❌ <b>Аккаунт не найден</b>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        await callback.answer()
        return
    
    await callback.message.edit_text("🔄 <b>Получаю последний код...</b>", parse_mode="HTML")
    
    session_path = os.path.join(SESSIONS_DIR, account['session_file'])
    
    if not os.path.exists(session_path):
        await callback.message.edit_text(
            "❌ <b>Файл сессии не найден</b>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        await callback.answer()
        return
    
    def extract_5digit_code(text: str) -> Optional[str]:
        pattern = r'\b(\d{5})\b'
        matches = re.findall(pattern, text)
        return matches[0] if matches else None
    
    client = None
    try:
        session_name = os.path.splitext(account['session_file'])[0]
        
        client = Client(
            name=session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSIONS_DIR,
            in_memory=False
        )
        
        await client.start()
        
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
                f"📱 <b>Аккаунт:</b> {account['phone_number']}\n\n"
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
    
    stats = await db.get_stats()
    
    text = (
        f"📊 <b>Статистика магазина</b>\n\n"
        f"👥 <b>Всего пользователей:</b> {stats['total_users']}\n"
        f"💰 <b>Всего продаж:</b> {stats['total_sales']}\n"
        f"💵 <b>Общая выручка:</b> {stats['total_revenue']}₽\n"
        f"📅 <b>Продаж сегодня:</b> {stats['today_sales']}\n"
        f"💵 <b>Выручка сегодня:</b> {stats['today_revenue']}₽\n\n"
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
    
    async with db.pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    
    sent = 0
    failed = 0
    
    status_msg = await message.answer("🔄 <b>Начинаю рассылку...</b>", parse_mode="HTML")
    
    for user in users:
        user_id = user['user_id']
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
        
        user = await db.get_user(user_id)
        if not user:
            await message.answer("❌ <b>Пользователь не найден</b>", parse_mode="HTML")
            await state.clear()
            return
        
        await state.update_data(target_user_id=user_id)
        await message.answer(
            f"💰 <b>Текущий баланс пользователя:</b> {user['balance']}₽\n\n"
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
        
        await db.update_balance(user_id, amount, "admin")
        new_balance = await db.get_balance(user_id)
        
        await message.answer(
            f"✅ <b>Баланс пользователя {user_id} изменен</b>\n"
            f"💰 <b>Новый баланс:</b> {new_balance}₽",
            parse_mode="HTML"
        )
        
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

# Массовое добавление аккаунтов
@dp.callback_query(F.data == "admin_bulk_add")
async def admin_bulk_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "📦 <b>Массовое добавление аккаунтов</b>\n\n"
        "Отправьте ZIP архив с session файлами (до 100 файлов).\n\n"
        "Каждый session файл должен быть в формате Pyrogram.\n\n"
        "После загрузки бот автоматически определит номера всех аккаунтов.",
        parse_mode="HTML"
    )
    await state.set_state(BulkAddStates.waiting_for_zip)
    await callback.answer()

@dp.message(BulkAddStates.waiting_for_zip, F.document)
async def admin_bulk_add_zip(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    document = message.document
    file_name = document.file_name
    
    if not file_name.endswith('.zip'):
        await message.answer("❌ <b>Пожалуйста, отправьте ZIP архив</b>", parse_mode="HTML")
        return
    
    status_msg = await message.answer("⏳ <b>Загружаю архив...</b>", parse_mode="HTML")
    
    temp_workdir = tempfile.mkdtemp(dir=TEMP_DIR)
    zip_path = os.path.join(temp_workdir, file_name)
    
    try:
        await bot.download(document, zip_path)
        
        await status_msg.edit_text("🔄 <b>Обрабатываю session файлы...</b>", parse_mode="HTML")
        
        successful, errors = await SessionManager.process_multiple_sessions(zip_path, temp_workdir)
        
        if not successful:
            await status_msg.edit_text(
                f"❌ <b>Не удалось обработать ни одного файла</b>\n\n"
                f"Ошибки:\n{chr(10).join(errors[:5])}",
                parse_mode="HTML"
            )
            return
        
        await state.update_data(
            accounts=successful,
            errors=errors
        )
        
        text = (
            f"✅ <b>Обработано {len(successful)} аккаунтов</b>\n\n"
            f"❌ Ошибок: {len(errors)}\n\n"
            f"Теперь выберите категорию для этих аккаунтов:"
        )
        
        await status_msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.admin_categories()
        )
        await state.set_state(BulkAddStates.waiting_for_category)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Ошибка:</b> {str(e)}", parse_mode="HTML")
        logger.error(f"Error in bulk add: {e}", exc_info=True)
    finally:
        shutil.rmtree(temp_workdir, ignore_errors=True)

@dp.callback_query(BulkAddStates.waiting_for_category, F.data.startswith("admin_cat_"))
async def admin_bulk_add_category(callback: CallbackQuery, state: FSMContext):
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
        "🌍 <b>Введите страну для всех аккаунтов</b> (например: Россия, Украина, США):",
        parse_mode="HTML"
    )
    await state.set_state(BulkAddStates.waiting_for_country)
    await callback.answer()

@dp.message(BulkAddStates.waiting_for_country)
async def admin_bulk_add_country(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    country = message.text.strip()
    await state.update_data(country=country)
    
    await message.answer(
        "💰 <b>Введите цену для всех аккаунтов</b> (в рублях):",
        parse_mode="HTML"
    )
    await state.set_state(BulkAddStates.waiting_for_price)

@dp.message(BulkAddStates.waiting_for_price)
async def admin_bulk_add_price(message: Message, state: FSMContext):
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
        accounts = data['accounts']
        errors = data.get('errors', [])
        
        accounts_data = []
        for acc in accounts:
            accounts_data.append({
                'category': data['category'],
                'country': data['country'],
                'phone_number': acc['phone_number'],
                'session_file': acc['session_file'],
                'price': price,
                'added_by': message.from_user.id
            })
        
        added, db_errors = await db.add_shop_accounts_bulk(accounts_data)
        
        text = (
            f"✅ <b>Массовое добавление завершено!</b>\n\n"
            f"📊 <b>Результат:</b>\n"
            f"• Всего обработано: {len(accounts)}\n"
            f"• Успешно добавлено: {added}\n"
            f"• Ошибок при обработке: {len(errors)}\n"
            f"• Ошибок БД: {len(db_errors)}\n\n"
            f"📂 <b>Категория:</b> {data['category']}\n"
            f"🌍 <b>Страна:</b> {data['country']}\n"
            f"💰 <b>Цена:</b> {price}₽\n"
        )
        
        if errors:
            text += f"\n❌ <b>Ошибки обработки:</b>\n"
            text += chr(10).join(errors[:5])
            if len(errors) > 5:
                text += f"\n... и еще {len(errors) - 5} ошибок"
        
        if db_errors:
            text += f"\n\n❌ <b>Ошибки БД:</b>\n"
            text += chr(10).join(db_errors[:5])
        
        await message.answer(text, parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ <b>Неверный формат цены. Введите число:</b>", parse_mode="HTML")
        return
    
    await state.clear()

# Управление изображениями
@dp.callback_query(F.data == "admin_images")
async def admin_images_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "🖼 <b>Управление изображениями</b>\n\n"
        "Выберите категорию для загрузки изображения:",
        parse_mode="HTML",
        reply_markup=Keyboards.admin_image_categories()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("img_"))
async def admin_image_select(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Доступ запрещен")
        return
    
    image_type = callback.data
    
    if image_type == "img_list":
        await show_image_list(callback)
        return
    
    image_names = {
        "img_welcome": "🏠 Приветствие",
        "img_profile": "👤 Профиль",
        "img_account_select": "📱 Выбор аккаунта",
        "img_account_give": "✅ Выдача аккаунта"
    }
    
    image_name = image_names.get(image_type, image_type)
    
    await state.update_data(image_type=image_type)
    await callback.message.edit_text(
        f"🖼 <b>Загрузка изображения для:</b> {image_name}\n\n"
        f"Отправьте изображение, которое будет отображаться вместе с сообщением.\n\n"
        f"<i>Поддерживаются форматы: JPEG, PNG</i>",
        parse_mode="HTML"
    )
    await state.set_state(ImageStates.waiting_for_image)
    await callback.answer()

@dp.message(ImageStates.waiting_for_image, F.photo)
async def admin_image_upload(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ <b>Доступ запрещен</b>", parse_mode="HTML")
        await state.clear()
        return
    
    data = await state.get_data()
    image_type = data['image_type']
    
    file_id = message.photo[-1].file_id
    
    await db.save_image(image_type, file_id)
    
    image_names = {
        "img_welcome": "Приветствие",
        "img_profile": "Профиль",
        "img_account_select": "Выбор аккаунта",
        "img_account_give": "Выдача аккаунта"
    }
    
    image_name = image_names.get(image_type, image_type)
    
    await message.answer(
        f"✅ <b>Изображение для '{image_name}' сохранено!</b>\n\n"
        f"Теперь оно будет отображаться в соответствующих сообщениях.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder().button(text="◀️ Назад", callback_data="admin_images").as_markup()
    )
    await state.clear()

async def show_image_list(callback: CallbackQuery):
    """Показывает список загруженных изображений"""
    images = await db.get_all_image_types()
    
    image_names = {
        "img_welcome": "🏠 Приветствие",
        "img_profile": "👤 Профиль",
        "img_account_select": "📱 Выбор аккаунта",
        "img_account_give": "✅ Выдача аккаунта"
    }
    
    if not images:
        text = "📭 <b>Нет загруженных изображений</b>"
    else:
        text = "🖼 <b>Загруженные изображения:</b>\n\n"
        for img in images:
            name = image_names.get(img, img)
            text += f"• {name}\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="◀️ Назад", callback_data="admin_images")
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

# Добавление одного аккаунта
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
        
        result, error = await SessionManager.process_session_file(file_path, file_name, temp_workdir)
        
        if error:
            await status_msg.edit_text(f"❌ <b>{error}</b>", parse_mode="HTML")
            return
        
        account_info, error = await SessionManager.get_account_info(
            result['session_path'],
            result['session_name'],
            result['session_dir']
        )
        
        if error:
            await status_msg.edit_text(f"❌ <b>Ошибка при входе:</b> {error}", parse_mode="HTML")
            return
        
        phone_number = account_info['phone_number']
        
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
        
        await db.add_shop_account(
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
    print("🤖 Бот VEST ACCOUNTS запускается...")
    
    # Подключаемся к базе данных
    await db.connect()
    print("✅ Подключение к PostgreSQL установлено")
    
    print(f"📁 Сессии хранятся в: {SESSIONS_DIR}")
    print(f"🖼 Изображения хранятся в БД")
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
