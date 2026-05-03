"""Хендлери сповіщень про оновлення бібліотеки.

Обробляє кнопки з broadcast-повідомлення (`utils/broadcast.py`):
- "👀 Показати додані тайтли" / next / prev — рендерить картку нового аніме
  з пагінацією по пачці.
- "🔕 Не сповіщати про оновлення" — opt-out (`bot_users.notify_updates=FALSE`).
"""
import json
import uuid
from collections import OrderedDict

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

import texts as t
from api.hikka_client import HikkaClient
from database.connection import db, transaction
from database.queries import SELECT_SYNC_BATCH, UPDATE_USER_NOTIFY_OFF
from utils.callbacks import MenuCB, NotifyCB
from utils.ui_shared import format_caption, kb_for_anime

router = Router()

# cb_id -> (batch_id, index, total). Дозволяє відновити notify-клавіатуру
# коли юзер тисне "Де дивитись" → "Назад", щоб не вискакувати з пачки.
_NOTIFY_CTX_CACHE_MAX = 500
_notify_ctx_cache: "OrderedDict[str, tuple[str, int, int]]" = OrderedDict()


def _set_notify_ctx(cb_id: str, batch_id: str, index: int, total: int) -> None:
    if cb_id in _notify_ctx_cache:
        _notify_ctx_cache.move_to_end(cb_id)
    _notify_ctx_cache[cb_id] = (batch_id, index, total)
    while len(_notify_ctx_cache) > _NOTIFY_CTX_CACHE_MAX:
        _notify_ctx_cache.popitem(last=False)


def try_get_notify_kb(cb_id: str) -> InlineKeyboardMarkup | None:
    """Повертає notify-клавіатуру для cb_id, якщо ця картка була відрендерена
    у notify-режимі. Інакше None — caller може використати дефолтний шлях."""
    ctx = _notify_ctx_cache.get(cb_id)
    if ctx is None:
        return None
    batch_id, index, total = ctx
    return _kb_with_nav(cb_id, batch_id, index, total)


def _kb_with_nav(cb_id: str, batch_id: str, index: int, total: int) -> InlineKeyboardMarkup:
    """Стандартна клавіатура картки + ряд навігації по пачці + 'до рекомендацій'."""
    base_kb = kb_for_anime(cb_id, has_filter=False)
    rows = list(base_kb.inline_keyboard)

    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(InlineKeyboardButton(
            text=t.BTN_NOTIFY_PREV,
            callback_data=NotifyCB(action="prev", batch_id=batch_id, index=index - 1).pack(),
        ))
    nav.append(InlineKeyboardButton(
        text=f"{index + 1}/{total}",
        callback_data="noop",
    ))
    if index < total - 1:
        nav.append(InlineKeyboardButton(
            text=t.BTN_NOTIFY_NEXT,
            callback_data=NotifyCB(action="next", batch_id=batch_id, index=index + 1).pack(),
        ))
    rows.append(nav)
    rows.append([InlineKeyboardButton(
        text=t.BTN_NOTIFY_BACK_TO_RECS,
        callback_data=MenuCB(action="random").pack(),
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _load_anime_row(slug: str):
    return db().execute(
        "SELECT slug, title, genres_json, score, year, episodes_total, "
        "poster_url, hikka_url, ua_poster_url, content_type, season "
        "FROM anime_library WHERE slug=%s",
        (slug,),
    ).fetchone()


@router.callback_query(NotifyCB.filter(F.action.in_({"show", "next", "prev"})))
async def cb_notify_show(callback: CallbackQuery, callback_data: NotifyCB, db_funcs: dict):
    batch_row = db().execute(SELECT_SYNC_BATCH, (callback_data.batch_id,)).fetchone()
    if not batch_row:
        await callback.answer(t.ALERT_NOTIFY_BATCH_GONE, show_alert=True)
        return

    slugs_json, _items_count, _last_user_id = batch_row
    try:
        slugs = json.loads(slugs_json) or []
    except (TypeError, ValueError):
        slugs = []

    if not slugs:
        await callback.answer(t.ALERT_NOTIFY_BATCH_GONE, show_alert=True)
        return

    index = max(0, min(callback_data.index, len(slugs) - 1))
    slug = slugs[index]

    row = _load_anime_row(slug)
    if not row:
        # Тайтл був видалений із бази після створення пачки
        await callback.answer(t.ALERT_NOTIFY_BATCH_GONE, show_alert=True)
        return

    anime = HikkaClient._row_to_anime(row)

    # Стандартна процедура реєстрації картки (як у cb_random_anime)
    cb_id = uuid.uuid4().hex[:12]
    save_cb_map = db_funcs["save_cb_map"]
    with transaction():
        cb_id = save_cb_map(cb_id, anime)

    # Інлайн-імпорт щоб уникнути циклічного імпорту з UaAnimeRcmd.py
    from UaAnimeRcmd import _cache_anime, is_telegram_poster_error, remove_invalid_ua_poster
    _cache_anime(cb_id, anime)

    caption = format_caption(anime, max_length=1024)
    kb = _kb_with_nav(cb_id, callback_data.batch_id, index, len(slugs))
    _set_notify_ctx(cb_id, callback_data.batch_id, index, len(slugs))

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
            print(f"[NOTIFY] Failed to send/edit photo for {slug}: {e}")
            if has_ua_poster and is_telegram_poster_error(e):
                remove_invalid_ua_poster(slug)
            if has_ua_poster and anime.poster_url and anime.poster_url.strip().startswith("http"):
                try:
                    await send_or_edit(anime.poster_url.strip())
                    return
                except Exception as ex2:
                    print(f"[NOTIFY] Hikka fallback failed for {slug}: {ex2}")

    # Текстовий fallback
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(caption, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(caption, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(NotifyCB.filter(F.action == "off"))
async def cb_notify_off(callback: CallbackQuery):
    user_id = callback.from_user.id
    with transaction():
        db().execute(UPDATE_USER_NOTIFY_OFF, (user_id,))
    await callback.answer(t.ALERT_NOTIFY_DISABLED, show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
