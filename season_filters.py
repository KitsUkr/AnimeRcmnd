"""
Season Filter Module - фільтр аніме по сезонам випуску.
"""
import json
import time
from typing import List

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import db, transaction
from callbacks import SeasonCB
from ui_shared import Anime, format_caption, kb_for_anime
from safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup
from sql_queries import (
    SELECT_USER_STATE_SEASONS,
    INSERT_USER_STATE_SEASONS,
    SELECT_USER_STATE_EXCLUDED_SEASONS,
    INSERT_USER_STATE_EXCLUDED_SEASONS,
)

router = Router()

SEASON_MAP = {
    "winter": "❄️ Зима",
    "spring": "🌸 Весна",
    "summer": "☀️ Літо",
    "fall": "🍂 Осінь"
}

# ==================== БД ====================
def get_selected_seasons(user_id: int) -> List[str]:
    conn = db()
    row = conn.execute(SELECT_USER_STATE_SEASONS, (user_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []

def set_selected_seasons(user_id: int, seasons: List[str]) -> None:
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(INSERT_USER_STATE_SEASONS, (user_id, json.dumps(seasons), now))
    # Reset alert flag
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)

def clear_season_filter(user_id: int) -> None:
    """Очистити фільтр сезонів"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            UPDATE user_state 
            SET selected_seasons_json = NULL, excluded_seasons_json = NULL, updated_at = %s
            WHERE user_id = %s
            """,
            (now, user_id),
        )
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)

# ==================== UI ====================
def kb_season_filter(included: List[str]) -> InlineKeyboardMarkup:
    """
    Клавіатура для вибору сезонів.
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # Кнопки сезонів (по 2 в ряд)
    row = []
    for slug, name in SEASON_MAP.items():
        is_inc = slug in included
        
        prefix = "✅ " if is_inc else ""
        btn_text = f"{prefix}{name}"
        
        row.append(InlineKeyboardButton(text=btn_text, callback_data=SeasonCB(action="toggle", slug=slug).pack()))
        
        if len(row) == 2:
            kb.inline_keyboard.append(row)
            row = []
            
    if row:
        kb.inline_keyboard.append(row)
                
    # Дії
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="🗑️ Очистити", callback_data=SeasonCB(action="clear").pack()),
        InlineKeyboardButton(text="⬇️ Рекомендувати", callback_data=SeasonCB(action="recommend").pack()),
    ])
    
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="« Назад", callback_data="start:filters"),
    ])
    
    return kb

def get_season_menu_text() -> str:
    return (
        "🍂 <b>Фільтр по сезонам</b>\n\n"
        "Оберіть один або декілька сезонів, які вас цікавлять.\n"
        "Натисніть на сезон, щоб увімкнути або вимкнути його.\n\n"
        "<i>Аніме буде знайдено, якщо воно виходило в БУДЬ-ЯКИЙ з обраних сезонів.</i>"
    )

# ==================== Handlers ====================
@router.callback_query(F.data == "start:seasons")
async def cb_open_season_filter(c: CallbackQuery):
    """Відкрити меню фільтра по сезонам"""
    user_id = c.from_user.id
    
    # Exit genre menu state if we're coming from there
    from UaAnimeRcmd import exit_genre_menu
    exit_genre_menu(user_id)
    
    await c.answer()
    
    included = get_selected_seasons(user_id)
    
    text = get_season_menu_text()
    kb = kb_season_filter(included)
    
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb)
    else:
        await safe_edit_text(c.message, text, reply_markup=kb)

@router.callback_query(SeasonCB.filter(F.action == "toggle"))
async def cb_toggle_season(c: CallbackQuery, callback_data: SeasonCB):
    """Увімкнути/вимкнути сезон"""
    user_id = c.from_user.id
    slug = callback_data.slug
    
    if not slug:
        await c.answer()
        return
        
    included = get_selected_seasons(user_id)
    
    # Тогл включення
    if slug in included:
        included.remove(slug)
    else:
        included.append(slug)
    set_selected_seasons(user_id, included)
        
    kb = kb_season_filter(included)
    await safe_edit_reply_markup(c.message, reply_markup=kb)
    await c.answer()

@router.callback_query(SeasonCB.filter(F.action == "clear"))
async def cb_season_clear(c: CallbackQuery):
    """Очистити всі фільтри сезонів"""
    user_id = c.from_user.id
    
    clear_season_filter(user_id)
    
    kb = kb_season_filter([])
    await safe_edit_reply_markup(c.message, reply_markup=kb)
    await c.answer("Фільтр сезонів очищено! ✨", show_alert=True)

@router.callback_query(SeasonCB.filter(F.action == "recommend"))
async def cb_season_recommend(c: CallbackQuery, hikka_client, db_funcs: dict):
    from UaAnimeRcmd import cb_random_anime
    await cb_random_anime(c, hikka_client, db_funcs)
