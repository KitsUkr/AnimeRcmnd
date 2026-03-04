"""
Hikka OAuth — авторизація юзерів через Hikka та синхронізація watch list.

Flow:
1. Юзер натискає "Увійти в Hikka" → бот формує URL авторизації
2. Юзер переходить на hikka.io, логіниться, підтверджує доступ
3. Hikka перенаправляє юзера назад в бота (deep link з reference)
4. Бот обмінює reference на auth token і зберігає в БД
5. При натисканні "❤️ Обране" — бот додає аніме в "planned" на Hikka
"""

import aiohttp
from aiohttp import web
import os
from typing import Optional, Tuple
from database.connection import db, transaction
from dotenv import load_dotenv

load_dotenv()

HIKKA_API_BASE = os.getenv("HIKKA_API_BASE", "https://api.hikka.io").strip().rstrip("/")
HIKKA_CLIENT_REFERENCE = os.getenv("HIKKA_CLIENT_REFERENCE", "").strip()
HIKKA_CLIENT_SECRET = os.getenv("HIKKA_CLIENT_SECRET", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
HIKKA_REDIRECT_PORT = int(os.getenv("HIKKA_REDIRECT_PORT", "8723"))


# =========================
# SQL Queries
# =========================

_INSERT_HIKKA_TOKEN = """
    INSERT INTO hikka_tokens (user_id, hikka_token, hikka_username, created_at, updated_at)
    VALUES (%s, %s, %s, NOW(), NOW())
    ON CONFLICT (user_id) DO UPDATE SET
        hikka_token = EXCLUDED.hikka_token,
        hikka_username = EXCLUDED.hikka_username,
        updated_at = NOW()
"""

_SELECT_HIKKA_TOKEN = """
    SELECT hikka_token, hikka_username FROM hikka_tokens
    WHERE user_id = %s
"""

_DELETE_HIKKA_TOKEN = """
    DELETE FROM hikka_tokens WHERE user_id = %s
"""

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS hikka_tokens (
        user_id BIGINT PRIMARY KEY,
        hikka_token TEXT NOT NULL,
        hikka_username TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
"""


# =========================
# DB init
# =========================

def init_hikka_auth_db():
    """Створити таблицю hikka_tokens якщо не існує."""
    conn = db()
    conn.execute(_CREATE_TABLE)
    conn.commit()


# =========================
# DB helpers
# =========================

def save_hikka_token(user_id: int, token: str, username: Optional[str] = None):
    """Зберегти або оновити Hikka auth token для юзера."""
    conn = db()
    conn.execute(_INSERT_HIKKA_TOKEN, (user_id, token, username))
    conn.commit()


def get_hikka_token(user_id: int) -> Optional[Tuple[str, Optional[str]]]:
    """Отримати (token, username) для юзера, або None."""
    conn = db()
    row = conn.execute(_SELECT_HIKKA_TOKEN, (user_id,)).fetchone()
    if row:
        return (row[0], row[1])
    return None


def delete_hikka_token(user_id: int):
    """Видалити Hikka token (logout)."""
    conn = db()
    conn.execute(_DELETE_HIKKA_TOKEN, (user_id,))
    conn.commit()


def is_hikka_logged_in(user_id: int) -> bool:
    """Перевірити, чи юзер залогінений в Hikka."""
    return get_hikka_token(user_id) is not None


# =========================
# OAuth Flow
# =========================

class HikkaAuth:
    """Клас для OAuth-авторизації на Hikka та роботи з watch list."""

    def __init__(self, base_url: str = None, client_reference: str = None, client_secret: str = None):
        self.base_url = (base_url or HIKKA_API_BASE).rstrip("/")
        self.client_reference = client_reference or HIKKA_CLIENT_REFERENCE
        self.client_secret = client_secret or HIKKA_CLIENT_SECRET

    @property
    def is_configured(self) -> bool:
        """Чи налаштовані OAuth credentials."""
        return bool(self.client_reference and self.client_secret)

    def get_auth_url(self) -> str:
        """
        Повертає URL для авторизації юзера на Hikka.
        
        Hikka OAuth page потребує два query-параметри:
        - reference: client_reference нашого OAuth клієнта
        - scope: права, які ми запитуємо (comma-separated)
        
        Юзер переходить за цим URL, логіниться на Hikka, 
        підтверджує доступ для нашого клієнта, і Hikka перенаправляє 
        його назад на endpoint (deep link в бота) з request_reference.
        """
        return f"https://hikka.io/oauth?reference={self.client_reference}&scope=watchlist"

    async def exchange_token(self, request_reference: str) -> Optional[str]:
        """
        Обміняти request_reference на auth token.
        
        Args:
            request_reference: reference, отриманий від Hikka після авторизації юзера
            
        Returns:
            Auth token string або None при помилці
        """
        url = f"{self.base_url}/auth/token"
        payload = {
            "request_reference": request_reference,
            "client_secret": self.client_secret,
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("secret")
                    else:
                        text = await resp.text()
                        print(f"[HIKKA AUTH] Token exchange failed ({resp.status}): {text}")
                        return None
            except Exception as e:
                print(f"[HIKKA AUTH] Token exchange error: {e}")
                return None

    async def get_user_info(self, token: str) -> Optional[dict]:
        """
        Отримати інформацію про юзера за auth token.
        
        Returns:
            dict з полями username, avatar тощо, або None
        """
        url = f"{self.base_url}/user/me"
        headers = {"auth": token}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        print(f"[HIKKA AUTH] get_user_info failed ({resp.status})")
                        return None
            except Exception as e:
                print(f"[HIKKA AUTH] get_user_info error: {e}")
                return None

    async def validate_token(self, token: str) -> bool:
        """Перевірити чи token ще валідний."""
        info = await self.get_user_info(token)
        return info is not None

    async def add_to_planned(self, user_id: int, slug: str) -> Tuple[bool, str]:
        """
        Додати аніме в список «Заплановане» (planned) на Hikka.
        
        Args:
            user_id: Telegram user ID
            slug: slug аніме на Hikka
            
        Returns:
            (success: bool, message: str)
        """
        token_data = get_hikka_token(user_id)
        if not token_data:
            return (False, "not_logged_in")

        token, _ = token_data
        url = f"{self.base_url}/watch/{slug}"
        headers = {"auth": token}
        payload = {
            "status": "planned",
            "episodes": 0,
            "score": 0,
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.put(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        return (True, "ok")
                    elif resp.status == 401:
                        # Token expired або revoked
                        delete_hikka_token(user_id)
                        return (False, "token_expired")
                    elif resp.status == 400:
                        text = await resp.text()
                        # Можливо, аніме вже в списку — це не помилка
                        if "already" in text.lower():
                            return (True, "already_exists")
                        print(f"[HIKKA AUTH] add_to_planned 400: {text}")
                        return (False, "bad_request")
                    else:
                        text = await resp.text()
                        print(f"[HIKKA AUTH] add_to_planned {resp.status}: {text}")
                        return (False, f"error_{resp.status}")
            except Exception as e:
                print(f"[HIKKA AUTH] add_to_planned error: {e}")
                return (False, "network_error")

    async def login_user(self, user_id: int, request_reference: str) -> Tuple[bool, str]:
        """
        Повний flow логіну: обмін reference → отримання інфо → збереження token.
        
        Returns:
            (success: bool, message: str — username або опис помилки)
        """
        # 1. Обміняти reference на token
        token = await self.exchange_token(request_reference)
        if not token:
            return (False, "Не вдалося обміняти код на токен. Спробуй ще раз.")

        # 2. Отримати інфо про юзера
        user_info = await self.get_user_info(token)
        username = None
        if user_info:
            username = user_info.get("username")

        # 3. Зберегти token
        save_hikka_token(user_id, token, username)

        return (True, username or "невідомий")


# =========================
# OAuth Redirect Server
# =========================

def create_hikka_redirect_app() -> web.Application:
    """
    Створює aiohttp web-додаток для OAuth redirect.
    
    Hikka після авторизації перенаправляє юзера на:
        {endpoint}?reference={request_reference}
    
    Цей сервер приймає цей запит і перенаправляє юзера
    на Telegram deep link:
        https://t.me/{BOT_USERNAME}?start=hikka_{reference}
    
    Що запускає /start handler в боті для обміну reference на token.
    """

    async def hikka_callback(request: web.Request) -> web.Response:
        reference = request.query.get("reference", "")
        if not reference:
            return web.Response(text="Missing reference parameter", status=400)
        
        bot_username = BOT_USERNAME
        if not bot_username:
            return web.Response(text="Bot username not configured", status=500)

        # Перенаправляємо на Telegram deep link
        deep_link = f"https://t.me/{bot_username}?start=hikka_{reference}"
        print(f"[HIKKA REDIRECT] {reference[:8]}... -> {deep_link}")
        raise web.HTTPFound(location=deep_link)

    app = web.Application()
    app.router.add_get("/hikka-callback", hikka_callback)
    return app


async def run_hikka_redirect_server():
    """Запускає redirect сервер для Hikka OAuth."""
    if not BOT_USERNAME:
        print("[HIKKA REDIRECT] BOT_USERNAME not set, redirect server disabled")
        return
    
    app = create_hikka_redirect_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HIKKA_REDIRECT_PORT)
    await site.start()
    print(f"[HIKKA REDIRECT] Server started on port {HIKKA_REDIRECT_PORT}")
