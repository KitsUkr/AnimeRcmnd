"""
Безпечне редагування повідомлень Telegram.
Якщо повідомлення занадто старе (>48 год) — надсилає нове замість помилки.
"""
from aiogram.types import Message, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest


def _is_edit_error(e: TelegramBadRequest) -> bool:
    """Перевіряє чи помилка пов'язана з неможливістю редагування."""
    msg = str(e).lower()
    return any(phrase in msg for phrase in [
        "message can't be edited",
        "message to edit not found",
        "message is not modified",
        "message_id_invalid",
        "message can not be edited",
    ])


async def safe_edit_text(message: Message, text: str, **kwargs):
    """Намагається edit_text, при помилці — надсилає нове повідомлення."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if _is_edit_error(e):
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(text, **kwargs)
        else:
            raise


async def safe_edit_media(message: Message, media: InputMediaPhoto, reply_markup=None):
    """Намагається edit_media, при помилці — надсилає нове фото."""
    try:
        await message.edit_media(media=media, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if _is_edit_error(e):
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer_photo(
                photo=media.media,
                caption=media.caption,
                reply_markup=reply_markup,
                parse_mode=media.parse_mode,
            )
        else:
            raise


async def safe_edit_reply_markup(message: Message, reply_markup, fallback_text: str = None):
    """Намагається edit_reply_markup, при помилці — надсилає нове повідомлення з клавіатурою."""
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if _is_edit_error(e):
            # Не можемо надіслати тільки клавіатуру — потрібен текст
            text = fallback_text or "⬇️"
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(text, reply_markup=reply_markup)
        else:
            raise
