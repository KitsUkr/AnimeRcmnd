import time

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database.connection import db, transaction
from utils.callbacks import MenuCB
from utils.safe_edit import safe_edit_text
import texts as t

router = Router()


async def kb_filters_hub(user_id: int) -> InlineKeyboardMarkup:
    from UaAnimeRcmd import get_user_filters, has_any_filter

    f = await get_user_filters(user_id)
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
            [_btn(t.BTN_RATING, "start:rating", has_rating)],
            [
                InlineKeyboardButton(text=t.BTN_RESET_FILTERS, callback_data="filters:clear_all"),
                InlineKeyboardButton(text=t.BTN_SEARCH, callback_data=MenuCB(action="recommend").pack()),
            ],
            [InlineKeyboardButton(text=t.BTN_BACK, callback_data=MenuCB(action="back").pack())],
        ]
    )


@router.callback_query(F.data == "start:filters")
async def cb_open_filters_hub(c: CallbackQuery):
    from UaAnimeRcmd import exit_genre_menu

    await exit_genre_menu(c.from_user.id)
    await c.answer()
    kb = await kb_filters_hub(c.from_user.id)
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(t.FILTERS_HUB_TEXT, reply_markup=kb)
    else:
        await safe_edit_text(c.message, t.FILTERS_HUB_TEXT, reply_markup=kb)


@router.callback_query(F.data == "filters:clear_all")
async def cb_clear_all_filters(c: CallbackQuery):
    from UaAnimeRcmd import get_user_filters, has_any_filter

    user_id = c.from_user.id
    if not has_any_filter(await get_user_filters(user_id)):
        await c.answer(t.ALERT_CLEAR_FILTERS_INFO, show_alert=True)
        return

    async with transaction():
        await db().execute(
            """
            UPDATE user_state SET
                selected_genres_json = '[]', excluded_genres_json = '[]',
                genre_snapshot_json = '[]',
                selected_content_types_json = '[]', excluded_content_types_json = '[]',
                selected_seasons_json = NULL, excluded_seasons_json = NULL,
                year_from = NULL, year_to = NULL, rating_min = NULL,
                filter_alert_shown = 0, in_genre_menu = 0,
                genre_menu_message_id = NULL, updated_at = %s
            WHERE user_id = %s
            """,
            (int(time.time()), user_id),
        )

    kb = await kb_filters_hub(user_id)
    await safe_edit_text(c.message, t.FILTERS_HUB_TEXT, reply_markup=kb)
    await c.answer(t.ALERT_ALL_FILTERS_CLEARED, show_alert=True)
