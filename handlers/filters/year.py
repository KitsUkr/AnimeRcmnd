"""
Year Filter Module - фільтр аніме по рокам випуску.
Дозволяє вибрати діапазон років (від/до) для пошуку.
"""
import json
import time
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest

from database.connection import db, transaction
from utils.callbacks import YearCB
from utils.ui_shared import Anime, format_caption, kb_for_anime
from utils.safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup

router = Router()

# Діапазон років для вибору
MIN_YEAR = 1960
MAX_YEAR = datetime.now().year
YEARS_PER_PAGE = 15


# ==================== БД ====================
def get_year_from(user_id: int) -> Optional[int]:
    """Отримати рік 'від' для фільтра"""
    conn = db()
    row = conn.execute(
        "SELECT year_from FROM user_state WHERE user_id = %s", (user_id,)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def set_year_from(user_id: int, year: Optional[int]) -> None:
    """Встановити рік 'від' для фільтра"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, year_from, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                year_from = EXCLUDED.year_from,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, year, now),
        )
    # Reset alert flag
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)


def get_year_to(user_id: int) -> Optional[int]:
    """Отримати рік 'до' для фільтра"""
    conn = db()
    row = conn.execute(
        "SELECT year_to FROM user_state WHERE user_id = %s", (user_id,)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def set_year_to(user_id: int, year: Optional[int]) -> None:
    """Встановити рік 'до' для фільтра"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, year_to, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                year_to = EXCLUDED.year_to,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, year, now),
        )
    # Reset alert flag
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)


def clear_year_filter(user_id: int) -> None:
    """Очистити фільтр років"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            UPDATE user_state 
            SET year_from = NULL, year_to = NULL, updated_at = %s
            WHERE user_id = %s
            """,
            (now, user_id),
        )
    # Reset alert flag
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)


# ==================== UI ====================
def kb_year_filter(year_from: Optional[int], year_to: Optional[int], mode: str = "from", page: int = 0) -> InlineKeyboardMarkup:
    """
    Клавіатура для вибору років.
    mode: "from" - вибір року ВІД, "to" - вибір року ДО
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # Поточний вибір
    from_text = f"Від: {year_from}" if year_from else "Від: —"
    to_text = f"До: {year_to}" if year_to else "До: —"
    
    # Switcher між режимами
    if mode == "from":
        from_btn = InlineKeyboardButton(text=f"▶ {from_text}", callback_data=YearCB(action="mode", mode="from", page=page).pack())
        to_btn = InlineKeyboardButton(text=to_text, callback_data=YearCB(action="mode", mode="to", page=page).pack())
    else:
        from_btn = InlineKeyboardButton(text=from_text, callback_data=YearCB(action="mode", mode="from", page=page).pack())
        to_btn = InlineKeyboardButton(text=f"▶ {to_text}", callback_data=YearCB(action="mode", mode="to", page=page).pack())
    
    kb.inline_keyboard.append([from_btn, to_btn])
    
    # Список років з пагінацією
    all_years = list(range(MAX_YEAR, MIN_YEAR - 1, -1))  # Від нових до старих
    total_pages = (len(all_years) + YEARS_PER_PAGE - 1) // YEARS_PER_PAGE
    
    if page < 0:
        page = 0
    if page >= total_pages:
        page = max(0, total_pages - 1)
    
    start = page * YEARS_PER_PAGE
    end = start + YEARS_PER_PAGE
    current_years = all_years[start:end]
    
    # Кнопки років (3 в ряд)
    row = []
    for year in current_years:
        # Перевіряємо, чи цей рік вибраний
        if mode == "from" and year == year_from:
            text = f"✅ {year}"
        elif mode == "to" and year == year_to:
            text = f"✅ {year}"
        else:
            text = str(year)
        
        action = "set_from" if mode == "from" else "set_to"
        row.append(InlineKeyboardButton(
            text=text, 
            callback_data=YearCB(action=action, year=year, mode=mode, page=page).pack()
        ))
        
        if len(row) == 3:
            kb.inline_keyboard.append(row)
            row = []
    
    if row:
        kb.inline_keyboard.append(row)
    
    # Навігація
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="«", callback_data=YearCB(action="page", mode=mode, page=page-1).pack()))
    else:
        nav_row.append(InlineKeyboardButton(text="«", callback_data="noop:left"))
    
    nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop:page"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="»", callback_data=YearCB(action="page", mode=mode, page=page+1).pack()))
    else:
        nav_row.append(InlineKeyboardButton(text="»", callback_data="noop:right"))
    
    kb.inline_keyboard.append(nav_row)
    
    # Дії
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="🗑️ Очистити", callback_data=YearCB(action="clear").pack()),
    ])
    
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="« Назад", callback_data="start:filters"),
    ])
    
    return kb


def get_year_menu_text(year_from: Optional[int], year_to: Optional[int]) -> str:
    """Текст меню вибору років"""
    status_parts = []
    if year_from:
        status_parts.append(f"від {year_from}")
    if year_to:
        status_parts.append(f"до {year_to}")
    
    status = " ".join(status_parts) if status_parts else "не вибрано"
    
    return (
        f"📅 <b>Фільтр по рокам</b>\n\n"
        f"Поточний вибір: <i>{status}</i>\n\n"
        "• Натисніть <b>Від</b> щоб вибрати початковий рік\n"
        "• Натисніть <b>До</b> щоб вибрати кінцевий рік"
    )


# ==================== Handlers ====================
@router.callback_query(F.data == "start:years")
async def cb_open_year_filter(c: CallbackQuery):
    """Відкрити меню фільтра по рокам"""
    user_id = c.from_user.id
    
    await c.answer()
    
    year_from = get_year_from(user_id)
    year_to = get_year_to(user_id)
    
    text = get_year_menu_text(year_from, year_to)
    kb = kb_year_filter(year_from, year_to, mode="from", page=0)
    
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb)
    else:
        await safe_edit_text(c.message, text, reply_markup=kb)


@router.callback_query(YearCB.filter(F.action == "mode"))
async def cb_switch_year_mode(c: CallbackQuery, callback_data: YearCB):
    """Перемикання між режимами Від/До"""
    user_id = c.from_user.id
    mode = callback_data.mode
    page = callback_data.page
    
    year_from = get_year_from(user_id)
    year_to = get_year_to(user_id)
    
    text = get_year_menu_text(year_from, year_to)
    kb = kb_year_filter(year_from, year_to, mode=mode, page=page)
    
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(YearCB.filter(F.action == "page"))
async def cb_year_page(c: CallbackQuery, callback_data: YearCB):
    """Пагінація списку років"""
    user_id = c.from_user.id
    mode = callback_data.mode
    page = callback_data.page
    
    year_from = get_year_from(user_id)
    year_to = get_year_to(user_id)
    
    kb = kb_year_filter(year_from, year_to, mode=mode, page=page)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb)
    await c.answer()


@router.callback_query(YearCB.filter(F.action == "set_from"))
async def cb_set_year_from(c: CallbackQuery, callback_data: YearCB):
    """Встановити/скинути рік 'від'"""
    user_id = c.from_user.id
    year = callback_data.year
    page = callback_data.page
    
    # Отримуємо поточні значення
    current_year_from = get_year_from(user_id)
    year_to = get_year_to(user_id)
    
    # Toggle: якщо повторно натиснули на вже вибраний рік - скидаємо
    if current_year_from == year:
        set_year_from(user_id, None)
        year_from = None
    else:
        # Якщо year_from > year_to, автоматично коригуємо
        if year_to and year > year_to:
            set_year_to(user_id, year)
            year_to = year
        
        set_year_from(user_id, year)
        year_from = year
    
    text = get_year_menu_text(year_from, year_to)
    kb = kb_year_filter(year_from, year_to, mode="from", page=page)
    
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(YearCB.filter(F.action == "set_to"))
async def cb_set_year_to(c: CallbackQuery, callback_data: YearCB):
    """Встановити/скинути рік 'до'"""
    user_id = c.from_user.id
    year = callback_data.year
    page = callback_data.page
    
    # Отримуємо поточні значення
    year_from = get_year_from(user_id)
    current_year_to = get_year_to(user_id)
    
    # Toggle: якщо повторно натиснули на вже вибраний рік - скидаємо
    if current_year_to == year:
        set_year_to(user_id, None)
        year_to = None
    else:
        # Якщо year_to < year_from, автоматично коригуємо
        if year_from and year < year_from:
            set_year_from(user_id, year)
            year_from = year
        
        set_year_to(user_id, year)
        year_to = year
    
    text = get_year_menu_text(year_from, year_to)
    kb = kb_year_filter(year_from, year_to, mode="to", page=page)
    
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(YearCB.filter(F.action == "clear"))
async def cb_year_clear(c: CallbackQuery):
    """Очистити фільтр років"""
    user_id = c.from_user.id
    
    clear_year_filter(user_id)
    
    text = get_year_menu_text(None, None)
    kb = kb_year_filter(None, None, mode="from", page=0)
    
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer("Фільтр років очищено! ✨", show_alert=True)


@router.callback_query(YearCB.filter(F.action == "recommend"))
async def cb_year_recommend(c: CallbackQuery, hikka_client, db_funcs: dict):
    """Рекомендувати аніме з урахуванням фільтра років"""
    from UaAnimeRcmd import cb_random_anime
    await cb_random_anime(c, hikka_client, db_funcs)
