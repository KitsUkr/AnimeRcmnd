from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from callbacks import MenuCB
from safe_edit import safe_edit_text

router = Router()

def kb_filters_hub() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎭 Жанри", callback_data="start:genres"),
                InlineKeyboardButton(text="🎬 Тип контенту", callback_data="start:content_types"),
            ],
            [
                InlineKeyboardButton(text="📅 Рік", callback_data="start:years"),
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
    
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=kb_filters_hub())
    else:
        await safe_edit_text(c.message, text, reply_markup=kb_filters_hub())
