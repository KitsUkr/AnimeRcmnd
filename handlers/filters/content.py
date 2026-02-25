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
    from UaAnimeRcmd import cb_random_anime
    await cb_random_anime(c, hikka_client, db_funcs)

