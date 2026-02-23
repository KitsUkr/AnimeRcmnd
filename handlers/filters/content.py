import json
import time
from typing import List, Optional

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from database.connection import db, transaction
from utils.callbacks import ContentTypeCB, MenuCB
from utils.ui_shared import Anime, format_caption, kb_for_anime
from utils.safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup

router = Router()

# Маппінг типів контенту: slug -> українська назва
CONTENT_TYPE_MAP: dict[str, str] = {
    "tv": "Серіал",
    "movie": "Фільм",
    "special": "Спешл",
    "ova": "ОВА",
    "ona": "ONA",
    "music": "Музика",
}

# Порядок відображення типів
CONTENT_TYPES_ORDER = ["tv", "movie", "special", "ova", "ona", "music"]

# ==================== Helpers ====================
def get_slug_by_name(name: str) -> str:
    """Конвертує українську назву в slug"""
    for slug, content_name in CONTENT_TYPE_MAP.items():
        if content_name == name:
            return slug
    return name

def get_name_by_slug(slug: str) -> str:
    """Конвертує slug в українську назву"""
    return CONTENT_TYPE_MAP.get(slug, slug)

# ==================== БД ====================
def get_selected_content_types(user_id: int) -> List[str]:
    """Отримати вибрані типи контенту"""
    conn = db()
    row = conn.execute("SELECT selected_content_types_json FROM user_state WHERE user_id = %s", (user_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception as e:
        print(f"Error loading selected content types: {e}")
        return []

def set_selected_content_types(user_id: int, types: List[str]) -> None:
    """Зберегти вибрані типи контенту"""
    json_str = json.dumps(sorted(set(types)), ensure_ascii=False)
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, selected_content_types_json, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                selected_content_types_json = EXCLUDED.selected_content_types_json,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, json_str, now),
        )

def get_excluded_content_types(user_id: int) -> List[str]:
    """Отримати виключені типи контенту"""
    conn = db()
    row = conn.execute("SELECT excluded_content_types_json FROM user_state WHERE user_id = %s", (user_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception as e:
        print(f"Error loading excluded content types: {e}")
        return []

def set_excluded_content_types(user_id: int, types: List[str]) -> None:
    """Зберегти виключені типи контенту"""
    json_str = json.dumps(sorted(set(types)), ensure_ascii=False)
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, excluded_content_types_json, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                excluded_content_types_json = EXCLUDED.excluded_content_types_json,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, json_str, now),
        )

def toggle_content_type_slug(user_id: int, type_slug: str) -> None:
    """Tri-state toggle: Нейтрально -> Включено -> Виключено -> Нейтрально"""
    included = get_selected_content_types(user_id)
    excluded = get_excluded_content_types(user_id)
    
    if type_slug in included:
        # Включено -> Виключено
        included.remove(type_slug)
        if type_slug not in excluded:
            excluded.append(type_slug)
        
        set_selected_content_types(user_id, included)
        set_excluded_content_types(user_id, excluded)
        
    elif type_slug in excluded:
        # Виключено -> Нейтрально
        excluded.remove(type_slug)
        set_excluded_content_types(user_id, excluded)
        
    else:
        # Нейтрально -> Включено
        if type_slug in excluded:
            excluded.remove(type_slug)
            set_excluded_content_types(user_id, excluded)
            
        included.append(type_slug)
        set_selected_content_types(user_id, included)
    
    # Reset alert flag so user sees updated filter info
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)

def clear_selected_content_types(user_id: int) -> None:
    """Очистити вибрані типи"""
    set_selected_content_types(user_id, [])

def clear_excluded_content_types(user_id: int) -> None:
    """Очистити виключені типи"""
    set_excluded_content_types(user_id, [])

# ==================== UI ====================
def kb_content_types(included: List[str], excluded: List[str]) -> InlineKeyboardMarkup:
    """Клавіатура для вибору типів контенту"""
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # Кнопки типів (2 кнопки в ряд)
    row = []
    for slug in CONTENT_TYPES_ORDER:
        name = get_name_by_slug(slug)
        
        # Tri-state icon
        if slug in included:
            text = f"✅ {name}"
        elif slug in excluded:
            text = f"🚫 {name}"
        else:
            text = name
        
        callback_data = ContentTypeCB(action="toggle", slug=slug).pack()
        row.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        
        if len(row) == 2:
            kb.inline_keyboard.append(row)
            row = []
    
    if row:
        kb.inline_keyboard.append(row)
    
    # Кнопки дій
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="🗑️ Очистити", callback_data=ContentTypeCB(action="clear").pack()),
    ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="« Назад", callback_data="start:filters"),
    ])
    
    return kb

# ==================== Handlers ====================
@router.callback_query(F.data == "start:content_types")
async def cb_open_content_types(c: CallbackQuery):
    """Відкрити меню вибору типів контенту"""
    await c.answer()
    
    inc = get_selected_content_types(c.from_user.id)
    exc = get_excluded_content_types(c.from_user.id)
    
    text = (
        "🎬 <b>Керування Типами Контенту</b>\n\n"
        "• Натисніть <b>раз</b> (✅), щоб шукати тільки цей тип.\n"
        "• Натисніть <b>два</b> (🚫), щоб виключити його.\n"
        "• Натисніть <b>три</b>, щоб скинути вибір."
    )
    
    kb = kb_content_types(included=inc, excluded=exc)
    
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb)
    else:
        await safe_edit_text(c.message, text, reply_markup=kb)

@router.callback_query(ContentTypeCB.filter(F.action == "toggle"))
async def cb_toggle_content_type(c: CallbackQuery, callback_data: ContentTypeCB):
    """Toggle вибору типу контенту"""
    slug = callback_data.slug
    
    if not slug:
        await c.answer("Помилка: відсутній тип контенту")
        return
    
    toggle_content_type_slug(c.from_user.id, slug)
    
    inc = get_selected_content_types(c.from_user.id)
    exc = get_excluded_content_types(c.from_user.id)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb_content_types(included=inc, excluded=exc))

@router.callback_query(ContentTypeCB.filter(F.action == "clear"))
async def cb_content_type_clear(c: CallbackQuery):
    """Очистити всі фільтри по типах"""
    clear_selected_content_types(c.from_user.id)
    clear_excluded_content_types(c.from_user.id)
    
    # Reset alert flag so user sees updated filter info
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(c.from_user.id)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb_content_types(included=[], excluded=[]))
    
    await c.answer("Всі фільтри скинуто! ✨")

@router.callback_query(ContentTypeCB.filter(F.action == "recommend"))
async def cb_content_type_recommend(c: CallbackQuery, hikka_client, db_funcs: dict):
    """Рекомендувати аніме з урахуванням фільтрів по типах"""
    import uuid
    from handlers.filters.genre import get_selected_genres, get_excluded_genres, get_name_by_slug as get_genre_name
    
    user_id = c.from_user.id
    selected_types = get_selected_content_types(user_id)
    excluded_types = get_excluded_content_types(user_id)
    
    from utils.ui_shared import get_filter_alert_text
    from handlers.filters.genre import get_selected_genres, get_excluded_genres, GENRE_MAP
    
    selected_genres = get_selected_genres(user_id)
    excluded_genres = get_excluded_genres(user_id)

    if not selected_types and not selected_genres:
        await c.answer("Виберіть хоча б один тип контенту або жанр! 😅", show_alert=True)
        return

    alert_text = get_filter_alert_text(
        selected_genres=selected_genres,
        excluded_genres=excluded_genres,
        selected_types=selected_types,
        excluded_types=excluded_types,
        genre_names=GENRE_MAP,
        type_names=CONTENT_TYPE_MAP
    )

    if alert_text:
        await c.answer(alert_text, show_alert=True)
    else:
        await c.answer()
    
    get_excluded = db_funcs['get_excluded_ids']
    get_last = db_funcs['get_last_page']
    set_last = db_funcs['set_last_page']
    mark_seen = db_funcs['mark_seen']
    save_cb_map = db_funcs['save_cb_map']
    
    excluded_ids = get_excluded(user_id)
    last_page = get_last(user_id)
    
    # Конвертуємо slugs в names для пошуку
    from handlers.filters.genre import get_all_genres
    await get_all_genres(hikka_client)
    selected_genre_names = [get_genre_name(s) for s in selected_genres]
    
    try:
        anime, used_page = await hikka_client.random_anime(
            exclude_ids=excluded_ids,
            last_page=last_page,
            genres=selected_genres,
            genre_names=selected_genre_names,
            excluded_genres=excluded_genres,
            content_types=selected_types,
            excluded_content_types=excluded_types,
        )
    except Exception as e:
        from api.hikka_client import FilteredAnimeExhaustedError
        if isinstance(e, FilteredAnimeExhaustedError):
            from utils.ui_shared import build_filter_exhausted_message
            msg = build_filter_exhausted_message(e)
            try:
                if c.message.content_type == "photo":
                    await c.message.delete()
                    await c.message.answer(msg)
                else:
                    await c.message.edit_text(msg)
            except Exception:
                try:
                    await c.answer("Ви переглянули все аніме за цими фільтрами!", show_alert=True)
                except:
                    pass
            return
        error_msg = f"Не вдалося знайти аніме з такими фільтрами.\n\nПомилка: {e}"
        await safe_edit_text(c.message, error_msg)
        return
    
    cb_id = uuid.uuid4().hex[:12]
    
    with transaction():
        set_last(user_id, used_page)
        mark_seen(user_id, anime.id)
        save_cb_map(cb_id, anime)
    
    caption = format_caption(anime)
    kb = kb_for_anime(cb_id, has_filter=True)
    
    # Choose poster: UA > Hikka > None
    final_poster_url = anime.ua_poster_url if anime.ua_poster_url else anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)
    
    if final_poster_url:
        from aiogram.types import InputMediaPhoto
        try:
            await c.message.edit_media(InputMediaPhoto(media=final_poster_url, caption=caption), reply_markup=kb)
            return
        except Exception as e:
            print(f"[CONTENT_FILTER] ❌ Не вдалося відправити постер {final_poster_url}: {e}")
            
            # Якщо це помилка Telegram і це UA постер - видаляємо його з бази
            if has_ua_poster:
                from UaAnimeRcmd import is_telegram_poster_error, remove_invalid_ua_poster
                if is_telegram_poster_error(e):
                    remove_invalid_ua_poster(anime.slug)
            
            # Fallback на Hikka постер якщо це був битий UA постер
            if has_ua_poster and anime.poster_url:
                print(f"[FALLBACK] Пробую Hikka постер в типах контенту")
                try:
                    await c.message.edit_media(InputMediaPhoto(media=anime.poster_url, caption=caption), reply_markup=kb)
                    return
                except Exception as e2:
                    print(f"[FALLBACK] Hikka постер теж не вдався: {e2}")
    
    await safe_edit_text(c.message, caption, reply_markup=kb)

