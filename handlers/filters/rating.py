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
        InlineKeyboardButton(text="🗑️ Очистити", callback_data=RatingCB(action="clear").pack()),
    ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="« Назад", callback_data="start:filters"),
    ])

    return kb


def get_rating_menu_text(rating_min: Optional[float]) -> str:
    """Текст меню вибору рейтингу"""
    status = f"від {rating_min}" if rating_min is not None else "не вибрано"

    return (
        f"⭐ <b>Фільтр по рейтингу</b>\n\n"
        f"Поточний вибір: <i>{status}</i>\n\n"
        "Показувати тільки аніме з рейтингом (оцінкою) не нижче обраного значення."
    )


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
    await c.answer("Фільтр рейтингу очищено! ✨", show_alert=True)


@router.callback_query(RatingCB.filter(F.action == "recommend"))
async def cb_rating_recommend(c: CallbackQuery, hikka_client, db_funcs: dict):
    """Рекомендувати аніме з урахуванням фільтра рейтингу"""
    user_id = c.from_user.id

    rating_min = get_rating_min(user_id)

    from utils.ui_shared import get_filter_alert_text
    from handlers.filters.genre import get_selected_genres, get_excluded_genres, GENRE_MAP, get_name_by_slug
    from handlers.filters.content import get_selected_content_types, get_excluded_content_types, CONTENT_TYPE_MAP
    from handlers.filters.year import get_year_from, get_year_to
    from handlers.filters.season import get_selected_seasons, SEASON_MAP

    genres_inc = get_selected_genres(user_id)
    genres_exc = get_excluded_genres(user_id)
    types_inc = get_selected_content_types(user_id)
    types_exc = get_excluded_content_types(user_id)
    year_from = get_year_from(user_id)
    year_to = get_year_to(user_id)
    seasons = get_selected_seasons(user_id)

    alert_text = get_filter_alert_text(
        selected_genres=genres_inc,
        excluded_genres=genres_exc,
        selected_types=types_inc,
        excluded_types=types_exc,
        genre_names=GENRE_MAP,
        type_names=CONTENT_TYPE_MAP,
        year_from=year_from,
        year_to=year_to,
        seasons=seasons,
        season_names=SEASON_MAP,
        rating_min=rating_min,
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

    genre_names = [get_name_by_slug(s) for s in genres_inc]

    try:
        anime, used_page = await hikka_client.random_anime(
            exclude_ids=excluded_ids,
            last_page=last_page,
            genres=genres_inc,
            genre_names=genre_names,
            excluded_genres=genres_exc,
            content_types=types_inc,
            excluded_content_types=types_exc,
            year_from=year_from,
            year_to=year_to,
            seasons=seasons,
            score_min=rating_min,
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
        await safe_edit_text(c.message, f"Не вдалося знайти аніме за вказаними фільтрами.\n\nПомилка: {e}")
        return

    cb_id = uuid.uuid4().hex[:12]

    with transaction():
        set_last(user_id, used_page)
        mark_seen(user_id, anime.id)
        save_cb_map(cb_id, anime)

    caption = format_caption(anime)
    kb = kb_for_anime(cb_id, has_filter=True)

    final_poster_url = anime.ua_poster_url if anime.ua_poster_url else anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)

    if final_poster_url:
        try:
            await c.message.edit_media(InputMediaPhoto(media=final_poster_url, caption=caption), reply_markup=kb)
            return
        except Exception as e:
            print(f"[RATING] ❌ Не вдалося відправити постер {final_poster_url}: {e}")
            if has_ua_poster:
                from UaAnimeRcmd import is_telegram_poster_error, remove_invalid_ua_poster
                if is_telegram_poster_error(e):
                    remove_invalid_ua_poster(anime.slug)
            if has_ua_poster and anime.poster_url:
                try:
                    await c.message.edit_media(InputMediaPhoto(media=anime.poster_url, caption=caption), reply_markup=kb)
                    return
                except Exception as e2:
                    print(f"[RATING] Fallback постер теж не вдався: {e2}")

    await safe_edit_text(c.message, caption, reply_markup=kb)
