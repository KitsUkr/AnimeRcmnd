"""Спільні helper-функції для рендерингу аніме-картки.

Використовуються `handlers/recent.py` (команда /new) і будь-якими
іншими хендлерами, що показують картку аніме з пагінацією.
"""
import uuid
from typing import Optional

import aiohttp
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from api.hikka_client import HikkaClient, get_cached_details, set_cached_details
from database.connection import db, transaction
from utils.ui_shared import Anime, format_caption


def load_anime_row(slug: str):
    """Завантажує рядок anime_library за slug. None якщо не знайдено."""
    return db().execute(
        "SELECT slug, title, genres_json, score, year, episodes_total, "
        "poster_url, hikka_url, ua_poster_url, content_type, season "
        "FROM anime_library WHERE slug=%s",
        (slug,),
    ).fetchone()


async def _enrich_with_details(anime: Anime, hikka_client: HikkaClient) -> None:
    """Підтягує description / watch_links з кешу або API. Мутує переданий anime."""
    cached = get_cached_details(anime.slug)
    if cached:
        cached_desc, cached_links = cached
        if cached_desc and not anime.description:
            anime.description = cached_desc
        if cached_links and not anime.watch_links:
            anime.watch_links = cached_links

    if anime.description and anime.watch_links:
        return

    # Кеш порожній або застарів — тягнемо з API
    try:
        async with aiohttp.ClientSession() as session:
            await hikka_client.load_details(session, anime.slug)
        if hikka_client.description and not anime.description:
            anime.description = hikka_client.description
        if hikka_client.watch_links and not anime.watch_links:
            anime.watch_links = hikka_client.watch_links
        set_cached_details(anime.slug, anime.description, anime.watch_links or [])
    except Exception as e:
        print(f"[CARD] Failed to enrich {anime.slug} from API: {e}")


async def prepare_anime_card(
    slug: str, db_funcs: dict, hikka_client: HikkaClient
) -> Optional[tuple[Anime, str, str]]:
    """Готує дані картки: завантажує запис, збагачує описом/watch_links,
    реєструє cb_id, кешує anime, формує caption.
    Повертає (anime, cb_id, caption) або None якщо slug не знайдено."""
    row = load_anime_row(slug)
    if not row:
        return None

    anime = HikkaClient._row_to_anime(row)

    # Підтягуємо синопсис і watch_links (cache → API)
    await _enrich_with_details(anime, hikka_client)

    cb_id = uuid.uuid4().hex[:12]
    save_cb_map = db_funcs["save_cb_map"]
    with transaction():
        cb_id = save_cb_map(cb_id, anime)

    # Інлайн-імпорт щоб уникнути циклічного імпорту з UaAnimeRcmd.py
    from UaAnimeRcmd import _cache_anime
    _cache_anime(cb_id, anime)

    caption = format_caption(anime, max_length=1024)
    return anime, cb_id, caption


async def send_card_via_callback(
    callback: CallbackQuery,
    anime: Anime,
    slug: str,
    caption: str,
    kb: InlineKeyboardMarkup,
) -> None:
    """Редагує існуюче повідомлення (або видаляє+відправляє нове) під картку аніме.
    Обробляє fallback з UA-постера на Hikka-постер та текстовий fallback."""
    from UaAnimeRcmd import is_telegram_poster_error, remove_invalid_ua_poster

    final_poster_url = anime.ua_poster_url or anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)

    async def send_or_edit(poster_url: str) -> None:
        if callback.message.photo:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=kb,
            )
        else:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(poster_url, caption=caption, reply_markup=kb)

    if final_poster_url and final_poster_url.strip().startswith("http"):
        try:
            await send_or_edit(final_poster_url.strip())
            return
        except Exception as e:
            print(f"[CARD] Failed to send/edit photo for {slug}: {e}")
            if has_ua_poster and is_telegram_poster_error(e):
                remove_invalid_ua_poster(slug)
            if has_ua_poster and anime.poster_url and anime.poster_url.strip().startswith("http"):
                try:
                    await send_or_edit(anime.poster_url.strip())
                    return
                except Exception as ex2:
                    print(f"[CARD] Hikka fallback failed for {slug}: {ex2}")

    # Текстовий fallback
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(caption, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(caption, reply_markup=kb, parse_mode=ParseMode.HTML)


async def send_card_to_message(
    message: Message,
    anime: Anime,
    slug: str,
    caption: str,
    kb: InlineKeyboardMarkup,
) -> None:
    """Відправляє нове повідомлення з карткою аніме (для команд типу /new).
    Обробляє fallback з UA-постера на Hikka-постер та текстовий fallback."""
    from UaAnimeRcmd import is_telegram_poster_error, remove_invalid_ua_poster

    final_poster_url = anime.ua_poster_url or anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)

    if final_poster_url and final_poster_url.strip().startswith("http"):
        try:
            await message.answer_photo(
                final_poster_url.strip(), caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML
            )
            return
        except Exception as e:
            print(f"[CARD] Failed to send photo for {slug}: {e}")
            if has_ua_poster and is_telegram_poster_error(e):
                remove_invalid_ua_poster(slug)
            if has_ua_poster and anime.poster_url and anime.poster_url.strip().startswith("http"):
                try:
                    await message.answer_photo(
                        anime.poster_url.strip(), caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML
                    )
                    return
                except Exception as ex2:
                    print(f"[CARD] Hikka fallback failed for {slug}: {ex2}")

    await message.answer(caption, reply_markup=kb, parse_mode=ParseMode.HTML)
