import json
import time
import uuid
from typing import Optional

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest

from database.connection import db, transaction
from utils.callbacks import RatingCB
from utils.ui_shared import Anime, format_caption, kb_for_anime
from utils.safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup
import texts as t

router = Router()

# Діапазон рейтингів для вибору (1.0 - 10.0, крок 0.5)
RATING_OPTIONS = [5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0]


# ==================== БД ====================
def get_rating_min(user_id: int) -> Optional[float]:
    """Отримати мінімальний рейтинг для фільтра"""
    conn = db()
    row = conn.execute(
        "SELECT rating_min FROM user_state WHERE user_id = %s", (user_id,)
    ).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return float(row[0])
    except (ValueError, TypeError):
        return None


def set_rating_min(user_id: int, rating: Optional[float]) -> None:
    """Встановити мінімальний рейтинг для фільтра"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, rating_min, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                rating_min = EXCLUDED.rating_min,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, rating, now),
        )
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)


def clear_rating_filter(user_id: int) -> None:
    """Очистити фільтр рейтингу"""
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            UPDATE user_state 
            SET rating_min = NULL, updated_at = %s
            WHERE user_id = %s
            """,
            (now, user_id),
        )
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)


# ==================== UI ====================
def kb_rating_filter(rating_min: Optional[float]) -> InlineKeyboardMarkup:
    """Клавіатура для вибору мінімального рейтингу"""
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    # Кнопки рейтингів (3 в ряд)
    row = []
    for r in RATING_OPTIONS:
        if rating_min is not None and abs(r - rating_min) < 0.01:
            text = f"✅ {r}"
        else:
            text = str(r)
        row.append(InlineKeyboardButton(
            text=text,
            callback_data=RatingCB(action="set", rating=r).pack()
        ))
        if len(row) == 3:
            kb.inline_keyboard.append(row)
            row = []

    if row:
        kb.inline_keyboard.append(row)

    # Дії
    kb.inline_keyboard.append([
        InlineKeyboardButton(text=t.BTN_CLEAR_FILTER, callback_data=RatingCB(action="clear").pack()),
    ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text=t.BTN_BACK, callback_data="start:filters"),
    ])

    return kb


def get_rating_menu_text(rating_min: Optional[float]) -> str:
    """Текст меню вибору рейтингу"""
    status = f"від {rating_min}" if rating_min is not None else "не вибрано"

    return t.RATING_MENU_TEXT.format(status=status)


# ==================== Handlers ====================
@router.callback_query(F.data == "start:rating")
async def cb_open_rating_filter(c: CallbackQuery):
    """Відкрити меню фільтра по рейтингу"""
    user_id = c.from_user.id

    await c.answer()

    rating_min = get_rating_min(user_id)
    text = get_rating_menu_text(rating_min)
    kb = kb_rating_filter(rating_min)

    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb)
    else:
        await safe_edit_text(c.message, text, reply_markup=kb)


@router.callback_query(RatingCB.filter(F.action == "set"))
async def cb_set_rating(c: CallbackQuery, callback_data: RatingCB):
    """Встановити/скинути мінімальний рейтинг"""
    user_id = c.from_user.id
    rating = callback_data.rating

    current = get_rating_min(user_id)
    # Toggle: якщо повторно натиснули на вже вибраний рейтинг - скидаємо
    if current is not None and abs(current - rating) < 0.01:
        set_rating_min(user_id, None)
        rating_min = None
    else:
        set_rating_min(user_id, rating)
        rating_min = rating

    text = get_rating_menu_text(rating_min)
    kb = kb_rating_filter(rating_min)

    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(RatingCB.filter(F.action == "clear"))
async def cb_rating_clear(c: CallbackQuery):
    """Очистити фільтр рейтингу"""
    user_id = c.from_user.id

    clear_rating_filter(user_id)

    text = get_rating_menu_text(None)
    kb = kb_rating_filter(None)

    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer(t.ALERT_RATING_FILTER_CLEARED, show_alert=True)


@router.callback_query(RatingCB.filter(F.action == "recommend"))
async def cb_rating_recommend(c: CallbackQuery, hikka_client, db_funcs: dict):
    """Рекомендувати аніме з урахуванням фільтра рейтингу"""
    from UaAnimeRcmd import cb_random_anime
    await cb_random_anime(c, hikka_client, db_funcs)
