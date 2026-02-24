from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from utils.callbacks import MenuCB
from utils.safe_edit import safe_edit_text

router = Router()

def kb_filters_hub(user_id: int) -> InlineKeyboardMarkup:
    from handlers.filters.genre import get_selected_genres, get_excluded_genres
    from handlers.filters.content import get_selected_content_types, get_excluded_content_types
    from handlers.filters.year import get_year_from, get_year_to
    from handlers.filters.season import get_selected_seasons
    from handlers.filters.rating import get_rating_min

    has_genres = bool(get_selected_genres(user_id) or get_excluded_genres(user_id))
    has_types = bool(get_selected_content_types(user_id) or get_excluded_content_types(user_id))
    has_year = get_year_from(user_id) is not None or get_year_to(user_id) is not None
    has_seasons = bool(get_selected_seasons(user_id))
    has_rating = get_rating_min(user_id) is not None

    def _btn(label: str, callback: str, active: bool) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label,
            callback_data=callback,
            style="success" if active else None,
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("Жанри", "start:genres", has_genres),
                _btn("Тип контенту", "start:content_types", has_types),
            ],
            [
                _btn("Рік", "start:years", has_year),
                _btn("Сезон", "start:seasons", has_seasons),
            ],
            [
                _btn("Рейтинг", "start:rating", has_rating),
            ],
            [
                InlineKeyboardButton(text="🎲 Пошук", callback_data=MenuCB(action="recommend").pack()),
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data=MenuCB(action="back").pack())
            ]
        ]
    )

@router.callback_query(F.data == "start:filters")
async def cb_open_filters_hub(c: CallbackQuery):
    # Exit genre menu state if we're coming from there
    from UaAnimeRcmd import exit_genre_menu
    exit_genre_menu(c.from_user.id)
    
    await c.answer()
    text = (
        "🔍 <b>Фільтри пошуку</b>\n\n"
        "Оберіть, за якими критеріями ви хочете налаштувати пошук аніме:"
    )
    
    user_id = c.from_user.id
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb_filters_hub(user_id))
    else:
        await safe_edit_text(c.message, text, reply_markup=kb_filters_hub(user_id))
