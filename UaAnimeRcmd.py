import traceback
from collections import OrderedDict
from utils.ui_shared import Anime, format_caption, MAX_CAPTION, kb_for_anime, get_filter_alert_text
import texts as t
import asyncio
import json
from handlers.user_profile import router as profile_router
import os
import time
import uuid
from database.connection import db, transaction
from database.queries import (
    INSERT_USER_SEEN, SELECT_USER_SEEN_BY_USER, DELETE_USER_SEEN_BY_USER,
    INSERT_USER_FEEDBACK, INSERT_USER_STATE_LAST_PAGE, SELECT_USER_STATE_LAST_PAGE,
    INSERT_CB_MAP, SELECT_CB_MAP_BY_ID, SELECT_CB_MAP_FULL, UPDATE_USER_STATE_CLEAR_PAGE
)
from typing import Any, Dict, List, Optional, Tuple, Callable, Awaitable
from handlers.user_profile import send_profile
from handlers.admin_panel import router as admin_router, init_admin_db, touch_user, is_admin

from handlers.filters.genre import router as genre_router, get_selected_genres, get_name_by_slug, get_all_genres, get_excluded_genres, GENRE_MAP

from handlers.filters.content import router as content_type_router, get_selected_content_types, get_excluded_content_types, CONTENT_TYPE_MAP
from handlers.filters.year import router as year_router, get_year_from, get_year_to
from handlers.filters.season import router as season_router, get_selected_seasons, SEASON_MAP
from handlers.filters.rating import router as rating_router, get_rating_min
from handlers.filters.hub import router as filters_hub_router
from handlers.inline_handler import router as inline_router
from handlers.recent import router as recent_router
from handlers.library_broadcast import (
    router as notif_router,
    broadcast_library_update,
    get_notifications_enabled,
)
from api.hikka_client import HikkaClient, get_or_refresh_watch_links, AllAnimeSeenError, FilteredAnimeExhaustedError
from api.hikka_auth import HikkaAuth, init_hikka_auth_db, is_hikka_logged_in, run_hikka_redirect_server
from utils.safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup
from aiogram import BaseMiddleware
from utils.callbacks import MenuCB, AnimeCB, WatchCB, AdminCB, HikkaCB, SettingsCB, NotifCB

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HIKKA_API_BASE = os.getenv("HIKKA_API_BASE", "https://api.hikka.io").strip().rstrip("/")
HIKKA_API_TOKEN = os.getenv("HIKKA_API_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(t.ERROR_NO_BOT_TOKEN)

def init_db() -> None:
    """Таблиці створюються скриптом міграції migrate_sqlite_to_postgres.py,
    але нові колонки додаємо тут для зручності."""
    with transaction():
        conn = db()
        try:
            conn.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS selected_seasons_json text;")
            conn.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS excluded_seasons_json text;")
            conn.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS rating_min real;")
            conn.execute("ALTER TABLE anime_library ADD COLUMN IF NOT EXISTS season text;")
            conn.execute("ALTER TABLE anime_library ADD COLUMN IF NOT EXISTS inserted_at bigint;")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_anime_inserted_at "
                "ON anime_library(inserted_at DESC) WHERE inserted_at IS NOT NULL;"
            )
            conn.execute("ALTER TABLE cb_map ADD COLUMN IF NOT EXISTS season text;")
            conn.execute("ALTER TABLE cb_map ADD COLUMN IF NOT EXISTS watch_links_json text;")
            conn.execute("ALTER TABLE user_feedback ADD COLUMN IF NOT EXISTS ua_poster_url text;")
            conn.execute("ALTER TABLE user_feedback ADD COLUMN IF NOT EXISTS content_type text;")
        except Exception as e:
            print(f"Помилка створення колонок: {e}")


init_admin_db()
init_hikka_auth_db()

class DependencyMiddleware(BaseMiddleware):
    def __init__(self, hikka_client, start_text: str, kb_start_func, db_funcs: dict):
        super().__init__()
        self.hikka_client = hikka_client
        self.start_text = start_text
        self.kb_start_func = kb_start_func
        self.db_funcs = db_funcs

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, Dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        data["hikka_client"] = self.hikka_client
        data["start_text"] = self.start_text
        data["kb_start_func"] = self.kb_start_func
        data["db_funcs"] = self.db_funcs
        return await handler(event, data)

def mark_seen(user_id: int, anime_id: str) -> None:
    with transaction():
        conn = db()
        conn.execute(INSERT_USER_SEEN, (user_id, anime_id, int(time.time())))

def set_feedback(user_id: int, anime: "Anime", value: int) -> None:
    with transaction():
        conn = db()
        conn.execute(
            INSERT_USER_FEEDBACK,
            (
                user_id,
                anime.id,
                int(value),
                int(time.time()),
                anime.title,
                anime.poster_url,
                anime.hikka_url,
                anime.year,
                anime.score,
                anime.episodes_total,
                json.dumps(anime.genres or [], ensure_ascii=False),
                anime.description,
                json.dumps(anime.watch_links or [], ensure_ascii=False),
                anime.ua_poster_url,
                anime.content_type,
            ),
        )

def clear_history(user_id: int) -> None:
    with transaction():
        conn = db()
        conn.execute(DELETE_USER_SEEN_BY_USER, (user_id,))
        conn.execute(UPDATE_USER_STATE_CLEAR_PAGE, (int(time.time()), user_id))

def is_telegram_poster_error(error: Exception) -> bool:
    """Перевіряє чи є помилка пов'язана з проблемами Telegram при відправці постера"""
    error_msg = str(error).lower()
    telegram_errors = [
        "bad request",
        "wrong file identifier",
        "file is too big",
        "file reference",
        "file_id",
        "media group",
        "photo",
        "invalid file",
        "file not found",
    ]
    return any(err in error_msg for err in telegram_errors)

def remove_invalid_ua_poster(slug: str) -> None:
    """Видаляє невалідний UA постер з бази даних"""
    try:
        with transaction():
            conn = db()
            conn.execute(
                "UPDATE anime_library SET ua_poster_url = NULL WHERE slug = %s",
                (slug,)
            )
        print(f"[POSTER] Видалено невалідний UA постер для {slug}")


    except Exception as e:
        print(f"[POSTER] Помилка видалення UA постера для {slug}: {e}")


def get_user_filters(user_id: int) -> dict:
    """Завантажує ВСІ фільтри одним SQL-запитом. Повертає dict."""
    conn = db()
    row = conn.execute(
        """SELECT selected_genres_json, excluded_genres_json,
                  selected_content_types_json, excluded_content_types_json,
                  year_from, year_to,
                  selected_seasons_json, rating_min
           FROM user_state WHERE user_id = %s""",
        (user_id,)
    ).fetchone()
    
    if not row:
        return {
            "genres": [], "excluded_genres": [],
            "content_types": [], "excluded_content_types": [],
            "year_from": None, "year_to": None,
            "seasons": [], "rating_min": None,
        }
    
    def _parse_json_list(val):
        if not val:
            return []
        try:
            r = json.loads(val)
            return r if isinstance(r, list) else []
        except Exception:
            return []
    
    return {
        "genres": _parse_json_list(row[0]),
        "excluded_genres": _parse_json_list(row[1]),
        "content_types": _parse_json_list(row[2]),
        "excluded_content_types": _parse_json_list(row[3]),
        "year_from": int(row[4]) if row[4] is not None else None,
        "year_to": int(row[5]) if row[5] is not None else None,
        "seasons": _parse_json_list(row[6]),
        "rating_min": float(row[7]) if row[7] is not None else None,
    }

def has_any_filter(f: dict) -> bool:
    """Перевіряє чи є хоча б один активний фільтр."""
    return bool(
        f["genres"] or f["excluded_genres"]
        or f["content_types"] or f["excluded_content_types"]
        or f["year_from"] or f["year_to"]
        or f["seasons"] or (f["rating_min"] is not None)
    )

def get_excluded_ids(user_id: int) -> List[str]:
    conn = db()
    seen = conn.execute(
        "SELECT anime_id FROM user_seen WHERE user_id=%s", (user_id,)
    ).fetchall()
    return list({r[0] for r in seen})

def get_last_page(user_id: int) -> Optional[int]:
    conn = db()
    row = conn.execute(SELECT_USER_STATE_LAST_PAGE, (user_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None

def set_last_page(user_id: int, page: int) -> None:
    with transaction():
        conn = db()
        conn.execute(INSERT_USER_STATE_LAST_PAGE, (user_id, int(page), int(time.time())))

# Час неактивності в секундах, після якого знову показувати алерт про фільтри (15 хв)
FILTER_ALERT_INACTIVITY_SECONDS = 15 * 60

def get_filter_alert_shown(user_id: int) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT filter_alert_shown, last_recommendation_at FROM user_state WHERE user_id = %s",
        (user_id,)
    ).fetchone()
    
    if not row:
        return False  # User doesn't exist, show alert
    
    filter_alert_shown = bool(row[0]) if row[0] is not None else False
    last_recommendation_at = row[1] if row[1] is not None else None
    
    # If alert was never shown, need to show it
    if not filter_alert_shown:
        return False
    
    # If user was inactive for too long, reset and show alert again
    if last_recommendation_at is not None:
        inactive_seconds = int(time.time()) - last_recommendation_at
        if inactive_seconds >= FILTER_ALERT_INACTIVITY_SECONDS:
            return False  # Too long inactive, show alert again
    
    return True  # Alert was shown and user is still active

def set_filter_alert_shown(user_id: int) -> None:
    """Mark that the filter alert has been shown and update last recommendation time"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, filter_alert_shown, last_recommendation_at, updated_at)
            VALUES (%s, 1, %s, %s)
            ON CONFLICT(user_id) 
            DO UPDATE SET filter_alert_shown = 1, last_recommendation_at = EXCLUDED.last_recommendation_at, updated_at = EXCLUDED.updated_at
            """,
            (user_id, now, now)
        )

def update_last_recommendation_time(user_id: int) -> None:
    """Update the last recommendation timestamp to keep session active"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, last_recommendation_at, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) 
            DO UPDATE SET last_recommendation_at = EXCLUDED.last_recommendation_at, updated_at = EXCLUDED.updated_at
            """,
            (user_id, now, now)
        )

def reset_filter_alert(user_id: int) -> None:
    """Reset the filter alert flag (e.g., when filters are changed)"""
    with transaction():
        conn = db()
        conn.execute(
            """
            UPDATE user_state 
            SET filter_alert_shown = 0, updated_at = %s
            WHERE user_id = %s
            """,
            (int(time.time()), user_id)
        )

def is_in_genre_menu(user_id: int) -> bool:
    """Check if user is currently in the genre selection menu"""
    conn = db()
    row = conn.execute(
        "SELECT in_genre_menu FROM user_state WHERE user_id = %s",
        (user_id,)
    ).fetchone()
    return bool(row[0]) if row and row[0] is not None else False

def enter_genre_menu(user_id: int, message_id: int) -> None:
    """Mark that user entered the genre menu and store the message ID"""
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, in_genre_menu, genre_menu_message_id, updated_at)
            VALUES (%s, 1, %s, %s)
            ON CONFLICT(user_id) 
            DO UPDATE SET in_genre_menu = 1, genre_menu_message_id = EXCLUDED.genre_menu_message_id, updated_at = EXCLUDED.updated_at
            """,
            (user_id, message_id, int(time.time()))
        )

def exit_genre_menu(user_id: int) -> None:
    """Mark that user left the genre menu"""
    with transaction():
        conn = db()
        conn.execute(
            """
            UPDATE user_state 
            SET in_genre_menu = 0, genre_menu_message_id = NULL, updated_at = %s
            WHERE user_id = %s
            """,
            (int(time.time()), user_id)
        )

def get_genre_menu_message_id(user_id: int) -> Optional[int]:
    """Get the message ID of the genre menu for this user"""
    conn = db()
    row = conn.execute(
        "SELECT genre_menu_message_id FROM user_state WHERE user_id = %s",
        (user_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None

def is_genre_hint_shown(user_id: int) -> bool:
    """Check if genre menu hint was shown to this user"""
    conn = db()
    row = conn.execute(
        "SELECT genre_hint_shown FROM user_state WHERE user_id = %s",
        (user_id,)
    ).fetchone()
    return bool(row[0]) if row and row[0] is not None else False

def set_genre_hint_shown(user_id: int) -> None:
    """Mark that genre menu hint was shown to this user"""
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, genre_hint_shown, updated_at)
            VALUES (%s, 1, %s)
            ON CONFLICT(user_id) 
            DO UPDATE SET genre_hint_shown = 1, updated_at = EXCLUDED.updated_at
            """,
            (user_id, int(time.time()))
        )

def get_user_history(user_id: int, page: int = 0, per_page: int = 10) -> Tuple[List[Dict], int]:
    offset = page * per_page
    conn = db()
    
    row = conn.execute("SELECT COUNT(*) FROM user_seen WHERE user_id=%s", (user_id,)).fetchone()
    total = row[0] if row else 0
    
    if total == 0:
        return [], 0
    
    query = """
        SELECT s.anime_id, s.seen_at, 
               (SELECT title FROM anime_library l WHERE l.slug = s.anime_id LIMIT 1) as title,
               (SELECT cb_id FROM cb_map m WHERE m.anime_id = s.anime_id ORDER BY m.created_at DESC LIMIT 1) as cb_id
        FROM user_seen s
        WHERE s.user_id = %s 
        ORDER BY s.seen_at DESC
        LIMIT %s OFFSET %s
    """
    
    rows = conn.execute(query, (user_id, per_page, offset)).fetchall()
    
    items = []
    for r in rows:
        anime_id, seen_at, title, cb_id = r
        items.append({
            "anime_id": anime_id,
            "seen_at": seen_at,
            "title": title or t.NO_TITLE,
            "cb_id": cb_id
        })
        
    return items, total

# ✅ зберігаємо повний снапшот Anime у cb_map
def save_cb_map(cb_id: str, anime: "Anime") -> str:
    """Зберігає або оновлює запис в cb_map. Повертає cb_id (існуючий або новий)."""
    with transaction():
        conn = db()
        
        # Перевіряємо чи вже є запис для цього anime_id
        existing = conn.execute(
            "SELECT cb_id FROM cb_map WHERE anime_id = %s",
            (anime.id,)
        ).fetchone()
        
        if existing:
            # Оновлюємо існуючий запис, зберігаючи cb_id
            conn.execute(
                """
                UPDATE cb_map SET 
                    description = %s,
                    watch_links_json = %s,
                    created_at = %s
                WHERE anime_id = %s
                """,
                (
                    anime.description,
                    json.dumps(anime.watch_links or [], ensure_ascii=False),
                    int(time.time()),
                    anime.id,
                ),
            )
            return existing[0]  # Повертаємо існуючий cb_id
        else:
            # Створюємо новий запис
            conn.execute(
                INSERT_CB_MAP,
                (
                    cb_id,
                    anime.id,
                    anime.description,
                    json.dumps(anime.watch_links or [], ensure_ascii=False),
                    int(time.time()),
                ),
            )
            return cb_id  # Повертаємо новий cb_id

def load_cb_map(cb_id: str) -> Optional[Tuple[str, str]]:
    """Повертає (anime_id, title) або None."""
    conn = db()
    row = conn.execute(SELECT_CB_MAP_BY_ID, (cb_id,)).fetchone()

    if not row:
        return None

    anime_id, title = row
    return str(anime_id), str(title or anime_id)

# ✅ повністю відновлюємо Anime за cb_id (працює після рестарту)
def load_anime_by_cb(cb_id: str) -> Optional["Anime"]:
    conn = db()
    row = conn.execute(SELECT_CB_MAP_FULL, (cb_id,)).fetchone()

    if not row:
        return None

    (anime_id, title, poster_url, hikka_url,
     year, score, episodes_total, genres_json,
     description, ua_poster_url, content_type, season,
     watch_links_json) = row

    try:
        genres = json.loads(genres_json) if genres_json else []
        if not isinstance(genres, list):
            genres = []
    except Exception as e:
        print(f"Error loading genres: {e}")
        genres = []

    try:
        watch_links = json.loads(watch_links_json) if watch_links_json else []
        if not isinstance(watch_links, list):
            watch_links = []
    except Exception:
        watch_links = []

    return Anime(
        id=str(anime_id),
        slug=str(anime_id),
        title=str(title),
        year=int(year) if year is not None else None,
        score=float(score) if score is not None else None,
        genres=[str(x) for x in genres][:8],
        episodes_total=int(episodes_total) if episodes_total is not None else None,
        description=str(description) if description else None,
        poster_url=str(poster_url) if poster_url else None,
        hikka_url=str(hikka_url) if hikka_url else None,
        watch_links=watch_links,
        ua_poster_url=str(ua_poster_url) if ua_poster_url else None,
        content_type=str(content_type) if content_type else None,
    )

def resolve_anime_id_by_cb(cb_id: str) -> Optional[str]:
    a = anime_cache.get(cb_id)
    if a:
        return a.id
    row = load_cb_map(cb_id)
    return row[0] if row else None  # row = (anime_id, title)

# =========================
# Telegram UI
# =========================
START_TEXT = t.START_TEXT

CHANNEL_SUBSCRIBE_TEXT = t.CHANNEL_SUBSCRIBE_TEXT

def is_new_user(user_id: int) -> bool:
    """Перевіряє чи користувач новий (немає запису в bot_users)"""
    conn = db()
    row = conn.execute("SELECT 1 FROM bot_users WHERE user_id = %s", (user_id,)).fetchone()
    return row is None

def kb_channel_subscribe() -> InlineKeyboardMarkup:
    """Клавіатура з кнопкою підписки на канал + Продовжити"""
    buttons = []
    if CHANNEL_USERNAME:
        channel = CHANNEL_USERNAME.lstrip("@")
        buttons.append([
            InlineKeyboardButton(
                text=t.BTN_SUBSCRIBE_CHANNEL,
                url=f"https://t.me/{channel}",
                icon_custom_emoji_id="5771695636411847302"
            )
        ])
    buttons.append([
        InlineKeyboardButton(text=t.BTN_CONTINUE, callback_data="subscribe:continue")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_hikka_onboarding() -> InlineKeyboardMarkup:
    """Клавіатура онбордингу: запропонувати прив'язку Hikka або пропустити."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t.BTN_HIKKA_LINK_ACCOUNT,
                              callback_data="onboard:hikka_login")],
        [InlineKeyboardButton(text=t.BTN_SKIP,
                              callback_data="onboard:to_main")],
    ])

# =========================
# Keyboards & Handlers
# =========================

def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.BTN_RANDOM_ANIME, callback_data=MenuCB(action="random").pack())],
            [InlineKeyboardButton(text=t.BTN_PROFILE, callback_data=MenuCB(action="profile").pack()),
             InlineKeyboardButton(text=t.BTN_SETTINGS, callback_data=SettingsCB(action="menu").pack())],
            [InlineKeyboardButton(text=t.BTN_SEARCH_FILTERS, callback_data="start:filters")],
        ]
    )

def reply_kb_main() -> ReplyKeyboardMarkup:
    """Постійна клавіатура внизу чату"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t.BTN_HOME), KeyboardButton(text=t.BTN_PROFILE)]
        ],
        resize_keyboard=True
    )

def kb_watch_links(cb_id: str, links: List[Dict[str, str]], back_data: str | None = None, history_page: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # Розділяємо на звичайні сайти та торренти (Toloka)
    regular_links = []
    torrent_links = []
    
    for link in links:
        txt = str(link.get("text") or "Link")
        url = str(link.get("url") or "")
        if not url:
            continue
        if txt.lower() == "toloka":
            torrent_links.append(link)
        else:
            regular_links.append(link)
    
    if back_data is None:
        back_data = AnimeCB(action="back_to_list", id=cb_id).pack()

    if not regular_links and torrent_links:
        # Тільки торрент-посилання — показуємо Толоку напряму без доп. кнопки
        for link in torrent_links:
            txt = str(link.get("text") or "Toloka")
            url = str(link.get("url") or "")
            if url:
                kb.inline_keyboard.append([InlineKeyboardButton(text=txt, url=url)])
    else:
        # Звичайні сайти
        for link in regular_links:
            txt = str(link.get("text") or "Link")
            url = str(link.get("url") or "")
            if url:
                kb.inline_keyboard.append([InlineKeyboardButton(text=txt, url=url)])

        # Кнопка торрентів якщо є і звичайні, і Толока
        if torrent_links:
            if history_page is not None:
                # Для історії використовуємо HistoryCB
                torrents_cb = HistoryCB(action="torrents", cb_id=cb_id, page=history_page).pack()
            else:
                torrents_cb = AnimeCB(action="torrents", id=cb_id).pack()
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=t.BTN_TORRENTS, callback_data=torrents_cb)
            ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(text=t.BTN_BACK, callback_data=back_data)
    ])
    return kb

def kb_torrent_links(cb_id: str, links: List[Dict[str, str]], back_data: str | None = None) -> InlineKeyboardMarkup:
    """Клавіатура для торрент-посилань (Толока)"""
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # Header
    kb.inline_keyboard.append([
        InlineKeyboardButton(text=t.BTN_TORRENTS, callback_data=WatchCB(action="noop").pack())
    ])

    # Toloka links
    for link in links:
        txt = str(link.get("text") or "Toloka")
        url = str(link.get("url") or "")
        if url:
            kb.inline_keyboard.append([InlineKeyboardButton(text=txt, url=url)])

    # Back to watch menu
    if back_data is None:
        back_data = AnimeCB(action="watch", id=cb_id).pack()

    kb.inline_keyboard.append([
        InlineKeyboardButton(text=t.BTN_BACK, callback_data=back_data)
    ])
    return kb

# LRU-кеш з обмеженням на 500 елементів (щоб пам'ять не росла нескінченно)
ANIME_CACHE_MAX_SIZE = 500
anime_cache: OrderedDict[str, Anime] = OrderedDict()

def _cache_anime(cb_id: str, anime: Anime) -> None:
    """Додає аніме в кеш з автоматичним видаленням старих записів."""
    if cb_id in anime_cache:
        anime_cache.move_to_end(cb_id)
    anime_cache[cb_id] = anime
    while len(anime_cache) > ANIME_CACHE_MAX_SIZE:
        anime_cache.popitem(last=False)

# =========================
# Bot
# =========================
router = Router()
hikka = HikkaClient(HIKKA_API_BASE, HIKKA_API_TOKEN)

@router.callback_query(F.data.startswith("noop"))
async def cb_noop(c: CallbackQuery):
    """Global noop handler for generic static buttons"""
    await c.answer()

@router.message(Command("start"))
async def cmd_start(message: Message, start_text: str, kb_start_func, hikka_auth: HikkaAuth):
    user_id = message.from_user.id
    new_user = is_new_user(user_id)
    
    # Парсимо deep link payload (наприклад /start ad_instagram_feb)
    ad_source = None
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""
    
    # --- Hikka OAuth deep link: /start hikka_{reference} ---
    if payload.startswith("hikka_"):
        request_reference = payload[6:]  # "hikka_abc123" -> "abc123"
        if request_reference and hikka_auth.is_configured:
            # Видаляємо deep link повідомлення щоб не засмічувати чат
            try:
                await message.delete()
            except Exception:
                pass

            success, result = await hikka_auth.login_user(user_id, request_reference)

            # Формуємо текст результату
            if success:
                result_text = t.HIKKA_LOGIN_SUCCESS.format(username=result)
            else:
                result_text = t.HIKKA_LOGIN_FAIL.format(error=result)

            result_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t.BTN_BACK, callback_data=SettingsCB(action="menu").pack())],
            ])

            # Редагуємо оригінальне повідомлення-інструкцію (якщо збережено)
            from api.hikka_auth import pop_hikka_login_msg
            login_msg_id = pop_hikka_login_msg(user_id)
            if login_msg_id:
                try:
                    await message.bot.edit_message_text(
                        text=result_text,
                        chat_id=message.chat.id,
                        message_id=login_msg_id,
                        reply_markup=result_kb,
                    )
                except Exception:
                    # Fallback: повідомлення видалене або недоступне
                    await message.answer(result_text, reply_markup=result_kb)
            else:
                # Fallback: бот перезавантажився, message_id загублено
                await message.answer(result_text, reply_markup=result_kb)
        return
    
    if payload.startswith("ad_"):
        ad_source = payload[3:]  # "ad_instagram_feb" -> "instagram_feb"
    
    # Зберігаємо/оновлюємо користувача (ad_source зберігається тільки для нових)
    with transaction():
        touch_user(user_id, message.from_user.username, message.from_user.first_name, 
                   ad_source=ad_source if new_user else None)
    
    if new_user and CHANNEL_USERNAME:
        # Новий користувач — показуємо рекомендацію підписки (без reply-клавіатури)
        user = message.from_user
        name = user.first_name or (f"@{user.username}" if user.username else None)
        name_part = f", {name}" if name else ""
        await message.answer(CHANNEL_SUBSCRIBE_TEXT.format(name_part=name_part), reply_markup=kb_channel_subscribe())
    else:
        # Існуючий користувач — одразу стартове меню + reply-клавіатура
        await message.answer(t.KEYBOARD_ADDED, reply_markup=reply_kb_main())
        await message.answer(start_text, reply_markup=kb_start_func())

@router.message(Command("feedback"))
async def cmd_feedback(message: Message):
    channel = CHANNEL_USERNAME.lstrip("@") if CHANNEL_USERNAME else ""
    text = t.FEEDBACK_TEXT
    kb = None
    if channel:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t.BTN_GO_TO_CHANNEL, icon_custom_emoji_id="5771695636411847302", url=f"https://t.me/{channel}")]
        ])
    await message.answer(text, reply_markup=kb)

# Обробники для постійних reply-кнопок
@router.message(F.text == t.BTN_HOME)
async def btn_start(message: Message, start_text: str, kb_start_func):
    await message.answer(start_text, reply_markup=kb_start_func())

@router.message(F.text == t.BTN_PROFILE)
async def btn_profile(message: Message):
    await send_profile(message, message.from_user.id, edit=False)

@router.callback_query(F.data == "subscribe:continue")
async def cb_subscribe_continue(callback: CallbackQuery, start_text: str, kb_start_func, hikka_auth: HikkaAuth):
    """Обробник кнопки 'Продовжити' після повідомлення про підписку"""
    await callback.answer()

    if hikka_auth.is_configured:
        # Пропонуємо прив'язати Hikka перед головним меню (без reply-клавіатури).
        # Reply-клавіатура з'явиться лише коли юзер дійсно перейде в головне меню.
        await safe_edit_text(callback.message, t.HIKKA_ONBOARDING_TEXT,
                             reply_markup=kb_hikka_onboarding())
    else:
        # Hikka OAuth не налаштовано — одразу головне меню + reply-клавіатура
        await callback.message.answer(t.KEYBOARD_ADDED, reply_markup=reply_kb_main())
        await safe_edit_text(callback.message, start_text, reply_markup=kb_start_func())

@router.callback_query(F.data == "onboard:to_main")
async def cb_onboard_to_main(callback: CallbackQuery, start_text: str, kb_start_func):
    """Перехід в головне меню з онбордингу — активує reply-клавіатуру."""
    await callback.answer()
    await callback.message.answer(t.KEYBOARD_ADDED, reply_markup=reply_kb_main())
    await safe_edit_text(callback.message, start_text, reply_markup=kb_start_func())

@router.callback_query(F.data == "onboard:hikka_login")
async def cb_onboard_hikka_login(c: CallbackQuery, hikka_auth: HikkaAuth):
    """Hikka login під час онбордингу — кнопка Назад веде в головне меню."""
    if not hikka_auth.is_configured:
        await c.answer(t.ALERT_HIKKA_NOT_CONFIGURED, show_alert=True)
        return

    auth_url = hikka_auth.get_auth_url()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t.BTN_HIKKA_OPEN, url=auth_url)],
        [InlineKeyboardButton(text=t.BTN_BACK, callback_data="onboard:to_main")],
    ])

    await c.answer()
    await safe_edit_text(c.message, t.HIKKA_LOGIN_INSTRUCTIONS, reply_markup=kb)

    # Зберігаємо message_id, щоб після OAuth callback відредагувати це ж повідомлення
    from api.hikka_auth import save_hikka_login_msg
    save_hikka_login_msg(c.from_user.id, c.message.message_id)

@router.callback_query(MenuCB.filter(F.action == "back"))
async def cb_back(callback: CallbackQuery, start_text: str, kb_start_func):
    await safe_edit_text(callback.message, start_text, reply_markup=kb_start_func())

@router.callback_query(MenuCB.filter(F.action == "profile"))
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    await send_profile(callback.message, callback.from_user.id, edit=True)

@router.callback_query(F.data == "start:back") 
async def cb_profile_back(callback: CallbackQuery, start_text: str, kb_start_func):
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(start_text, reply_markup=kb_start_func())
    else:
        await safe_edit_text(callback.message, start_text, reply_markup=kb_start_func())

def kb_settings(user_id: int) -> InlineKeyboardMarkup:
    """Клавіатура меню Налаштувань"""
    notif_on = get_notifications_enabled(user_id)
    notif_text = t.BTN_SETTINGS_NOTIF_ON if notif_on else t.BTN_SETTINGS_NOTIF_OFF
    # Якщо увімкнено — клік веде на діалог підтвердження вимкнення (count=0 = з Settings).
    # Якщо вимкнено — клік одразу вмикає назад.
    notif_action = "disable_ask" if notif_on else "enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=notif_text, callback_data=NotifCB(action=notif_action, count=0).pack())],
            [InlineKeyboardButton(text=t.BTN_HIKKA_LINK_ACCOUNT, icon_custom_emoji_id="5292247247453457908", callback_data=HikkaCB(action="status").pack())],
            [InlineKeyboardButton(text=t.BTN_BACK, callback_data=SettingsCB(action="back").pack())],
        ]
    )

@router.callback_query(SettingsCB.filter(F.action == "menu"))
async def cb_settings_menu(callback: CallbackQuery):
    """Меню Налаштувань"""
    await callback.answer()
    text = t.SETTINGS_TEXT
    await safe_edit_text(callback.message, text, reply_markup=kb_settings(callback.from_user.id))

@router.callback_query(SettingsCB.filter(F.action == "back"))
async def cb_settings_back(callback: CallbackQuery, start_text: str, kb_start_func):
    """Повернення з Налаштувань до стартового меню"""
    await safe_edit_text(callback.message, start_text, reply_markup=kb_start_func())

@router.callback_query(MenuCB.filter(F.action == "recommend"))
async def cb_recommend_from_filters(callback: CallbackQuery, hikka_client: HikkaClient, db_funcs: dict):
    """Рекомендувати з хабу фільтрів — тільки якщо є хоча б один активний фільтр."""
    user_id = callback.from_user.id
    f = get_user_filters(user_id)

    if not has_any_filter(f):
        await callback.answer(t.ALERT_NEED_FILTER, show_alert=True)
        return
    await cb_random_anime(callback, hikka_client, db_funcs)

@router.callback_query(MenuCB.filter(F.action == "random"))
async def cb_random_anime(callback: CallbackQuery, hikka_client: HikkaClient, db_funcs: dict):
    user_id = callback.from_user.id
    
    get_excluded = db_funcs['get_excluded_ids']
    get_last = db_funcs['get_last_page']
    set_last = db_funcs['set_last_page']
    mark_seen = db_funcs['mark_seen']
    save_cb_map = db_funcs['save_cb_map']

    excluded_ids = get_excluded(user_id)
    last_page = get_last(user_id)
    
    
    f = get_user_filters(user_id)
    search_genres = f["genres"]
    excluded_genres = f["excluded_genres"]
    selected_content_types = f["content_types"]
    excluded_content_types = f["excluded_content_types"]
    year_from = f["year_from"]
    year_to = f["year_to"]
    seasons = f["seasons"]
    rating_min = f["rating_min"]
    
    # Ensure genre map is loaded for SQL search (names)
    await get_all_genres(hikka_client)
    search_genre_names = [get_name_by_slug(s) for s in search_genres]

    has_filters = has_any_filter(f)
    
    # Filter alert logic
    if has_filters and not get_filter_alert_shown(user_id):
        
        alert_text = get_filter_alert_text(
            selected_genres=search_genres,
            excluded_genres=excluded_genres,
            selected_types=selected_content_types,
            excluded_types=excluded_content_types,
            genre_names=GENRE_MAP,
            type_names=CONTENT_TYPE_MAP,
            year_from=year_from,
            year_to=year_to,
            seasons=seasons,
            season_names=SEASON_MAP,
            rating_min=rating_min,
        )
        if alert_text:
            await callback.answer(alert_text, show_alert=True)
            set_filter_alert_shown(user_id)

    # Core Logic
    try:
        anime, used_page = await hikka_client.random_anime(
            exclude_ids=excluded_ids,
            last_page=last_page,
            genres=search_genres,
            genre_names=search_genre_names,
            excluded_genres=excluded_genres,
            content_types=selected_content_types,
            excluded_content_types=excluded_content_types,
            year_from=year_from,
            year_to=year_to,
            seasons=seasons,
            score_min=rating_min,
        )
    except FilteredAnimeExhaustedError as e:
        from utils.ui_shared import build_filter_exhausted_message
        msg = build_filter_exhausted_message(e)
        try:
            if callback.message.content_type == "photo":
                await callback.message.delete()
                await callback.message.answer(msg)
            else:
                await callback.message.edit_text(msg)
        except Exception:
            try:
                await callback.answer(t.ALERT_FILTERED_EXHAUSTED, show_alert=True)
            except:
                pass
        return

    except AllAnimeSeenError as e:
        msg = t.ALL_ANIME_SEEN.format(total_count=e.total_count)
        try:
            if callback.message.content_type == "photo":
                await callback.message.delete()
                await callback.message.answer(msg)
            else:
                await callback.message.edit_text(msg)
        except Exception:
            try:
                await callback.answer(t.ALERT_ALL_ANIME_SEEN.format(total_count=e.total_count), show_alert=True)
            except:
                pass
        return

    except Exception as e:
        # Log technical details for debugging
        print(f"[ERROR] cb_random_anime failed for user {user_id}: {e}")
        traceback.print_exc()
        
        try:
            msg = t.ERROR_ANIME_SEARCH
            if callback.message.content_type == "photo":
               await callback.message.delete()
               await callback.message.answer(msg)
            else:
               await callback.message.edit_text(msg)
        except Exception:
            try:
                await callback.answer(t.ALERT_ERROR_ANIME_SEARCH, show_alert=True)
            except:
                pass
        return

    cb_id = uuid.uuid4().hex[:12]
    
    with transaction():
        touch_user(user_id, callback.from_user.username, callback.from_user.first_name)
        set_last(user_id, used_page)
        mark_seen(user_id, anime.id)
        # save_cb_map повертає існуючий cb_id якщо аніме вже є, або новий
        cb_id = save_cb_map(cb_id, anime)
        # Update last recommendation time to keep session active for filter alerts
        update_last_recommendation_time(user_id)
    
    _cache_anime(cb_id, anime)

    # Presentation Logic
    has_filters = bool(search_genres or excluded_genres or selected_content_types or excluded_content_types or year_from or year_to or seasons or rating_min is not None)
    caption_limit = 960 if has_filters else 1024
    
    caption = format_caption(anime, max_length=caption_limit)
    kb = kb_for_anime(cb_id, has_filter=has_filters)

    # Choose poster: UA > Hikka > None
    final_poster_url = anime.ua_poster_url if anime.ua_poster_url else anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)

    async def send_anime_message(poster_url_to_use):
        if callback.message.photo:
             await callback.message.edit_media(
                media=InputMediaPhoto(
                    media=poster_url_to_use,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                ),
                reply_markup=kb
            )
        else:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(poster_url_to_use, caption=caption, reply_markup=kb)

    if final_poster_url and final_poster_url.strip().startswith("http"):
        poster_clean = final_poster_url.strip()
        try:
            await send_anime_message(poster_clean)
            return
        except Exception as e:
            print(f"Failed to send/edit photo ({final_poster_url}): {e}")
            # Якщо це помилка Telegram і це UA постер - видаляємо його з бази
            if has_ua_poster and is_telegram_poster_error(e):
                remove_invalid_ua_poster(anime.slug)
            
            # Fallback to Hikka if UA failed
            if has_ua_poster and anime.poster_url and anime.poster_url.strip().startswith("http"):
                print(f"[FALLBACK] Пробую Hikka постер замість битого UA")
                try:
                    await send_anime_message(anime.poster_url.strip())
                    return
                except Exception as ex2:
                    print(f"[FALLBACK] Failed: {ex2}")


    # Fallback text
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(caption, reply_markup=kb)
    else:
        await safe_edit_text(callback.message, caption, reply_markup=kb)

@router.callback_query(AnimeCB.filter(F.action == "watch"))
async def cb_watch_list(callback: CallbackQuery, callback_data: AnimeCB, db_funcs: dict, hikka_client: HikkaClient):
    cb_id = callback_data.id
    load_map = db_funcs['load_cb_map']
    
    row = load_map(cb_id)
    if not row:
        await callback.answer(t.ALERT_DATA_STALE, show_alert=True)
        return

    anime_id, title = row
    watch_links = await get_or_refresh_watch_links(anime_id, hikka_client)
    if not watch_links:
        await callback.answer(t.ALERT_NO_LINKS, show_alert=True)
        return

    await callback.answer()
    await safe_edit_reply_markup(callback.message, reply_markup=kb_watch_links(cb_id, watch_links))

@router.callback_query(AnimeCB.filter(F.action == "torrents"))
async def cb_torrent_list(callback: CallbackQuery, callback_data: AnimeCB, db_funcs: dict, hikka_client: HikkaClient):
    """Показує список торрент-посилань (Толока)"""
    cb_id = callback_data.id
    load_map = db_funcs['load_cb_map']
    
    row = load_map(cb_id)
    if not row:
        await callback.answer(t.ALERT_DATA_STALE, show_alert=True)
        return

    anime_id, title = row
    watch_links = await get_or_refresh_watch_links(anime_id, hikka_client)
    
    # Фільтруємо тільки Toloka посилання
    torrent_links = [
        link for link in watch_links 
        if str(link.get("text") or "").lower() == "toloka"
    ]
    
    if not torrent_links:
        await callback.answer(t.ALERT_NO_TORRENT_LINKS, show_alert=True)
        return

    await callback.answer()
    await safe_edit_reply_markup(callback.message, reply_markup=kb_torrent_links(cb_id, torrent_links))

@router.callback_query(AnimeCB.filter(F.action == "back_to_list"))
async def cb_back_to_anime(callback: CallbackQuery, callback_data: AnimeCB):
    cb_id = callback_data.id
    user_id = callback.from_user.id

    # Якщо картка була відкрита з /new — повертаємо recent-клавіатуру
    # з пагінацією, щоб юзера не "викидало" з пачки.
    from handlers.recent import try_get_recent_kb
    recent_kb = try_get_recent_kb(cb_id)
    if recent_kb is not None:
        await callback.answer()
        await safe_edit_reply_markup(callback.message, reply_markup=recent_kb)
        return

    f = get_user_filters(user_id)
    has_filters = has_any_filter(f)

    await callback.answer()
    await safe_edit_reply_markup(callback.message, reply_markup=kb_for_anime(cb_id, has_filter=has_filters))

@router.callback_query(AnimeCB.filter(F.action == "like"))
async def cb_rate(callback: CallbackQuery, callback_data: AnimeCB, db_funcs: dict, hikka_auth: HikkaAuth):
    action = callback_data.action
    cb_id = callback_data.id
    val = 1 
    
    load_anime = db_funcs['load_anime_by_cb']
    anime = anime_cache.get(cb_id) or load_anime(cb_id)
    
    if not anime:
        await callback.answer(t.ALERT_DATA_STALE_DOT, show_alert=True)
        return

    set_fb = db_funcs['set_feedback']
    set_fb(callback.from_user.id, anime, val)

    # --- Hikka sync: додаємо в "planned" якщо юзер залогінений ---
    user_id = callback.from_user.id
    hikka_msg = ""
    if hikka_auth.is_configured and is_hikka_logged_in(user_id):
        try:
            success, status = await hikka_auth.add_to_planned(user_id, anime.slug)
            if success:
                hikka_msg = t.ALERT_HIKKA_SYNC_OK
            elif status == "token_expired":
                hikka_msg = t.ALERT_HIKKA_TOKEN_EXPIRED
            # Інші помилки ігноруємо (локальне збереження вже відбулось)
        except Exception as e:
            print(f"[HIKKA SYNC] Error for user {user_id}: {e}")
    await callback.answer(t.ALERT_ADDED_TO_FAVORITES.format(hikka_msg=hikka_msg))

# History Handlers
from utils.callbacks import HistoryCB

@router.callback_query(MenuCB.filter(F.action == "history"))
async def cb_menu_history(c: CallbackQuery, db_funcs: dict):
    await show_history_card(c, db_funcs, idx=0)

@router.callback_query(HistoryCB.filter(F.action == "page"))
async def cb_history_page(c: CallbackQuery, callback_data: HistoryCB, db_funcs: dict):
    idx = callback_data.page
    await show_history_card(c, db_funcs, idx=idx)

def kb_history_view(cb_id: str, idx: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=t.BTN_WHERE_TO_WATCH, callback_data=HistoryCB(action="watch", cb_id=cb_id, page=idx).pack()),
            InlineKeyboardButton(text=t.BTN_ADD_FAVORITE, callback_data=AnimeCB(action="like", id=cb_id).pack())
        ]
    ]
    
    # Navigation - кнопки завжди показуються, неактивні мають noop
    nav = []
    if total > 1 and idx > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=HistoryCB(action="page", page=idx-1).pack()))
    else:
        nav.append(InlineKeyboardButton(text="«", callback_data="noop:left"))
    
    # Кнопка сторінки: інтерактивна якщо > 10 сторінок
    if total > 10:
        nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data=HistoryCB(action="pages", page=idx).pack()))
    else:
        nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop:page"))
    
    if total > 1 and idx < total - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=HistoryCB(action="page", page=idx+1).pack()))
    else:
        nav.append(InlineKeyboardButton(text="»", callback_data="noop:right"))
        
    rows.append(nav)
    
    # Back to profile
    rows.append([InlineKeyboardButton(text=t.BTN_BACK_TO_PROFILE, callback_data=MenuCB(action="profile").pack())])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_history_pages_tiles(current_page: int, total: int, tiles_per_row: int = 5, step: int = 10) -> InlineKeyboardMarkup:
    """Генерує клавіатуру з плитками для швидкої навігації (кожні 10 сторінок)"""
    rows = []
    
    # Заголовок
    rows.append([InlineKeyboardButton(text=t.BTN_GO_TO_PAGE, callback_data="noop:header")])
    
    # Генеруємо плитки кожні 10 сторінок: 10, 20, 30...
    tiles = []
    
    # Перша сторінка (1)
    if current_page == 0:
        tiles.append(InlineKeyboardButton(text="[1]", callback_data=HistoryCB(action="page", page=0).pack()))
    else:
        tiles.append(InlineKeyboardButton(text="1", callback_data=HistoryCB(action="page", page=0).pack()))
    
    # Кожні 10 сторінок (10, 20, 30...)
    for page_num in range(step - 1, total, step):  # 9, 19, 29... (0-indexed)
        display_num = page_num + 1  # 10, 20, 30...
        
        if page_num == current_page:
            text = f"[{display_num}]"
        else:
            text = str(display_num)
        
        tiles.append(InlineKeyboardButton(
            text=text, 
            callback_data=HistoryCB(action="page", page=page_num).pack()
        ))
    
    # Остання сторінка якщо не кратна 10
    last_page = total - 1
    if last_page > 0 and (last_page + 1) % step != 0:
        if last_page == current_page:
            tiles.append(InlineKeyboardButton(text=f"[{total}]", callback_data=HistoryCB(action="page", page=last_page).pack()))
        else:
            tiles.append(InlineKeyboardButton(text=str(total), callback_data=HistoryCB(action="page", page=last_page).pack()))
    
    # Групуємо плитки по рядках
    for i in range(0, len(tiles), tiles_per_row):
        rows.append(tiles[i:i + tiles_per_row])
    
    # Кнопка назад
    rows.append([InlineKeyboardButton(
        text=t.BTN_BACK, 
        callback_data=HistoryCB(action="page", page=current_page).pack()
    )])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(HistoryCB.filter(F.action == "pages"))
async def cb_history_pages(c: CallbackQuery, callback_data: HistoryCB, db_funcs: dict):
    """Показує плитки сторінок для швидкої навігації"""
    user_id = c.from_user.id
    current_page = callback_data.page
    
    # Отримуємо загальну кількість записів
    get_hist = db_funcs['get_user_history']
    _, total = get_hist(user_id, page=0, per_page=1)
    
    await c.answer()
    
    # Показуємо плитки
    kb = kb_history_pages_tiles(current_page, total)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb)

@router.callback_query(HistoryCB.filter(F.action == "watch"))
async def cb_history_watch(c: CallbackQuery, callback_data: HistoryCB, db_funcs: dict, hikka_client: HikkaClient):
    cb_id = callback_data.cb_id
    page_idx = callback_data.page
    
    load_map = db_funcs['load_cb_map']
    row = load_map(cb_id)
    if not row:
        await c.answer(t.ALERT_DATA_STALE, show_alert=True)
        return

    anime_id, title = row
    watch_links = await get_or_refresh_watch_links(anime_id, hikka_client)
    if not watch_links:
        await c.answer(t.ALERT_NO_LINKS, show_alert=True)
        return

    await c.answer()
    
    # Generate back button that returns to history page at specific index
    back_data = HistoryCB(action="page", page=page_idx).pack()
    
    await safe_edit_reply_markup(c.message, reply_markup=kb_watch_links(cb_id, watch_links, back_data=back_data, history_page=page_idx))

@router.callback_query(HistoryCB.filter(F.action == "torrents"))
async def cb_history_torrents(c: CallbackQuery, callback_data: HistoryCB, db_funcs: dict, hikka_client: HikkaClient):
    """Показує торрент-посилання в історії"""
    cb_id = callback_data.cb_id
    page_idx = callback_data.page
    
    load_map = db_funcs['load_cb_map']
    row = load_map(cb_id)
    if not row:
        await c.answer(t.ALERT_DATA_STALE, show_alert=True)
        return

    anime_id, title = row
    watch_links = await get_or_refresh_watch_links(anime_id, hikka_client)
    
    # Фільтруємо тільки Toloka посилання
    torrent_links = [
        link for link in watch_links 
        if str(link.get("text") or "").lower() == "toloka"
    ]
    
    if not torrent_links:
        await c.answer(t.ALERT_NO_TORRENT_LINKS, show_alert=True)
        return

    await c.answer()
    
    # Back to watch menu with history page
    back_data = HistoryCB(action="watch", cb_id=cb_id, page=page_idx).pack()
    await safe_edit_reply_markup(c.message, reply_markup=kb_torrent_links(cb_id, torrent_links, back_data=back_data))

async def show_history_card(c: CallbackQuery, db_funcs: dict, idx: int):
    user_id = c.from_user.id
    get_hist = db_funcs['get_user_history']
    load_anime = db_funcs['load_anime_by_cb']
    
    # Get 1 item at offset idx
    items, total = get_hist(user_id, page=idx, per_page=1)
    
    if not items:
        if total == 0:
             await c.answer(t.ALERT_HISTORY_EMPTY, show_alert=True)
        else:
             # Should not happen if idx is valid, but safeguard
             await c.answer(t.ALERT_HISTORY_LOAD_ERROR, show_alert=True)
        return

    item = items[0]
    cb_id = item['cb_id']
    
    if not cb_id:
         # Fallback if we have record but no cb_map entry (rare)
         await c.answer(t.ALERT_DATA_PARTIALLY_LOST, show_alert=True)
         return

    anime = load_anime(cb_id)
    if not anime:
        await c.answer(t.ALERT_DATA_LOST, show_alert=True)
        return

    header = t.HISTORY_HEADER.format(current=idx+1, total=total)
    
    safe_limit = 1000 - len(header)
    
    caption = format_caption(anime, max_length=safe_limit)
    full_caption = header + caption

    kb = kb_history_view(cb_id, idx, total)

    # Choose poster
    final_poster = anime.ua_poster_url if anime.ua_poster_url else anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)

    # Send/Edit
    if final_poster:
        try:
             await c.message.edit_media(
                media=InputMediaPhoto(media=final_poster, caption=full_caption),
                reply_markup=kb
            )
             return
        except Exception as e:
            print(f"[HISTORY] Failed to send poster ({final_poster}): {e}")
            # Якщо це помилка Telegram і це UA постер - видаляємо його з бази
            if has_ua_poster and is_telegram_poster_error(e):
                remove_invalid_ua_poster(anime.slug)
            # Fallback на Hikka постер якщо це був битий UA постер
            if has_ua_poster and anime.poster_url and anime.poster_url.strip().startswith("http"):
                print(f"[FALLBACK] Пробую Hikka постер в історії")
                try:
                    await c.message.edit_media(
                        media=InputMediaPhoto(media=anime.poster_url, caption=full_caption),
                        reply_markup=kb
                    )
                    return
                except Exception as e2:
                    print(f"[FALLBACK] Hikka постер теж не вдався: {e2}")
    # Fallback to delete/send if type mismatch or error
    try:
        await c.message.delete()
    except Exception:
        pass
        
    if final_poster:
        # Спробуємо answer_photo з fallback
        poster_to_use = final_poster
        if has_ua_poster and anime.poster_url:
            # Можливо UA постер битий, спробуємо Hikka
            try:
                await c.message.answer_photo(final_poster, caption=full_caption, reply_markup=kb)
                return
            except Exception as e:
                print(f"[FALLBACK] UA постер не працює при answer_photo, пробую Hikka")

                # Якщо це помилка Telegram - видаляємо UA постер з бази
                if is_telegram_poster_error(e):
                    remove_invalid_ua_poster(anime.slug)
                poster_to_use = anime.poster_url
        
        try:
            await c.message.answer_photo(poster_to_use, caption=full_caption, reply_markup=kb)
        except Exception as e:
            print(f"[HISTORY] Всі постери failed, показую текст: {e}")
            # Якщо це помилка Telegram і це UA постер - видаляємо його з бази
            if has_ua_poster and is_telegram_poster_error(e):
                remove_invalid_ua_poster(anime.slug)
            await c.message.answer(full_caption, reply_markup=kb)
    else:
        await c.message.answer(full_caption, reply_markup=kb)

@router.callback_query(WatchCB.filter())
async def cb_watch_noop(callback: CallbackQuery):
    await callback.answer()

async def refresh_library_loop(bot: Bot):
    # Delay first run to let bot start
    await asyncio.sleep(10)
    while True:
        try:
            new_slugs = await hikka.sync_library()
            if new_slugs:
                await broadcast_library_update(bot, len(new_slugs))
        except Exception as e:
            print(f"[LIBRARY] Sync loop error: {e}")
        # Run every 24 hours
        await asyncio.sleep(24 * 3600)

@router.message(Command("force_sync"))
async def cmd_force_sync(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(t.SYNC_STARTED, parse_mode=ParseMode.HTML)

    try:
        # full=True щоб точно оновити все, включаючи постери
        new_slugs = await hikka.sync_library(full=True)
        await message.answer(t.SYNC_COMPLETED, parse_mode=ParseMode.HTML)
        if new_slugs:
            await broadcast_library_update(message.bot, len(new_slugs))
    except Exception as e:
        # Log technical details for debugging
        print(f"[ERROR] Force sync failed: {e}")
        traceback.print_exc()
        await message.answer(t.SYNC_ERROR, parse_mode=ParseMode.HTML)

@router.callback_query(AdminCB.filter(F.action == "force_sync"))
async def cb_admin_force_sync(c: CallbackQuery, hikka_client: HikkaClient):
    """Примусова синхронізація бібліотеки через адмін-панель"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
        return

    await c.answer(t.ALERT_SYNC_STARTING)

    try:
        # Відправляємо повідомлення про початок синхронізації
        await c.message.answer(t.SYNC_STARTED, parse_mode=ParseMode.HTML)

        # full=True щоб точно оновити все, включаючи постери
        new_slugs = await hikka_client.sync_library(full=True)
        await c.message.answer(t.SYNC_COMPLETED, parse_mode=ParseMode.HTML)
        if new_slugs:
            await broadcast_library_update(c.bot, len(new_slugs))
    except Exception as e:
        # Log technical details for debugging
        print(f"[ERROR] Force sync failed: {e}")
        traceback.print_exc()
        await c.message.answer(t.SYNC_ERROR, parse_mode=ParseMode.HTML)

# Запуск поллинга
async def main() -> None:
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Start background tasks
    _ = asyncio.create_task(refresh_library_loop(bot))
    _ = asyncio.create_task(run_hikka_redirect_server())

    dp = Dispatcher()

    hikka_auth = HikkaAuth()
    
    dp.workflow_data.update(
        hikka_client=hikka,
        hikka_auth=hikka_auth,
        start_text=START_TEXT,
        kb_start_func=kb_start,
        db_funcs={
            "get_excluded_ids": get_excluded_ids,
            "get_last_page": get_last_page,
            "set_last_page": set_last_page,
            "mark_seen": mark_seen,
            "save_cb_map": save_cb_map,
            "load_cb_map": load_cb_map,
            "load_anime_by_cb": load_anime_by_cb,
            "set_feedback": set_feedback,
            "get_user_history": get_user_history,
        }
    )
    
    print("[DEBUG] Реєструємо роутери...")
    dp.include_router(router)
    dp.include_router(profile_router)
    dp.include_router(admin_router)
    dp.include_router(recent_router)
    dp.include_router(genre_router)
    dp.include_router(content_type_router)
    dp.include_router(year_router)
    dp.include_router(season_router)
    dp.include_router(rating_router)
    dp.include_router(filters_hub_router)
    dp.include_router(inline_router)
    dp.include_router(notif_router)
    print(f"[DEBUG] Роутерів зареєстровано: {len(dp.sub_routers)}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
