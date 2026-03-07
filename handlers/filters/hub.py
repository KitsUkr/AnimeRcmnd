from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from utils.callbacks import MenuCB
from utils.safe_edit import safe_edit_text
import texts as t

router = Router()

def kb_filters_hub(user_id: int) -> InlineKeyboardMarkup:
    from UaAnimeRcmd import get_user_filters
    f = get_user_filters(user_id)

    has_genres = bool(f["genres"] or f["excluded_genres"])
    has_types = bool(f["content_types"] or f["excluded_content_types"])
    has_year = f["year_from"] is not None or f["year_to"] is not None
    has_seasons = bool(f["seasons"])
    has_rating = f["rating_min"] is not None

    def _btn(label: str, callback: str, active: bool) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label,
            callback_data=callback,
            style="success" if active else None,
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(t.BTN_GENRES, "start:genres", has_genres),
                _btn(t.BTN_CONTENT_TYPE, "start:content_types", has_types),
            ],
            [
                _btn(t.BTN_YEAR, "start:years", has_year),
                _btn(t.BTN_SEASON, "start:seasons", has_seasons),
            ],
            [
                _btn(t.BTN_RATING, "start:rating", has_rating),
            ],
            [
                InlineKeyboardButton(text=t.BTN_RESET_FILTERS, callback_data="filters:clear_all"),
                InlineKeyboardButton(text=t.BTN_SEARCH, callback_data=MenuCB(action="recommend").pack()),
            ],
            [
                InlineKeyboardButton(text=t.BTN_BACK, callback_data=MenuCB(action="back").pack())
            ]
        ]
    )

@router.callback_query(F.data == "start:filters")
async def cb_open_filters_hub(c: CallbackQuery):
    # Exit genre menu state if we're coming from there
    from UaAnimeRcmd import exit_genre_menu
    exit_genre_menu(c.from_user.id)
    
    await c.answer()
    text = t.FILTERS_HUB_TEXT
    
    user_id = c.from_user.id
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb_filters_hub(user_id))
    else:
        await safe_edit_text(c.message, text, reply_markup=kb_filters_hub(user_id))


@router.callback_query(F.data == "filters:clear_all")
async def cb_clear_all_filters(c: CallbackQuery):
    """
    Глобальне очищення всіх фільтрів користувача.
    """
    from handlers.filters.genre import (
        clear_selected_genres,
        clear_excluded_genres,
        set_genre_snapshot,
        get_selected_genres,
        get_excluded_genres,
    )
    from handlers.filters.content import (
        clear_selected_content_types,
        clear_excluded_content_types,
        get_selected_content_types,
        get_excluded_content_types,
    )
    from handlers.filters.season import clear_season_filter, get_selected_seasons
    from handlers.filters.year import clear_year_filter, get_year_from, get_year_to
    from handlers.filters.rating import clear_rating_filter, get_rating_min
    from UaAnimeRcmd import reset_filter_alert, exit_genre_menu

    user_id = c.from_user.id

    # Перевірка: чи є хоч один активний фільтр
    has_genres = bool(get_selected_genres(user_id) or get_excluded_genres(user_id))
    has_types = bool(get_selected_content_types(user_id) or get_excluded_content_types(user_id))
    has_year = get_year_from(user_id) is not None or get_year_to(user_id) is not None
    has_seasons = bool(get_selected_seasons(user_id))
    has_rating = get_rating_min(user_id) is not None

    if not any([has_genres, has_types, has_year, has_seasons, has_rating]):
        await c.answer(t.ALERT_CLEAR_FILTERS_INFO, show_alert=True)
        return

    # Вихід з меню жанрів, якщо користувач там
    exit_genre_menu(user_id)

    # Очищення по всіх типах фільтрів
    clear_selected_genres(user_id)
    clear_excluded_genres(user_id)
    set_genre_snapshot(user_id, [])

    clear_selected_content_types(user_id)
    clear_excluded_content_types(user_id)

    clear_season_filter(user_id)
    clear_year_filter(user_id)
    clear_rating_filter(user_id)

    # Скинути попередження по фільтрам
    reset_filter_alert(user_id)

    # Оновити хаб фільтрів
    text = t.FILTERS_HUB_TEXT

    await safe_edit_text(c.message, text, reply_markup=kb_filters_hub(user_id))
    await c.answer(t.ALERT_ALL_FILTERS_CLEARED, show_alert=True)
