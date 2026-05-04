"""Хендлер команди /new — показує аніме, додані до бази протягом
поточного циклу синхронізації (≈12 днів від моменту виклику).

Семантика "нового" базується на колонці `anime_library.inserted_at`,
яка проставляється тільки при INSERT (ON CONFLICT її не змінює).
Поріг — `LIBRARY_SYNC_INTERVAL - 1 day`, що відтворює TTL "до дня
перед наступним повним синком" без потреби scheduled task.
"""
import time
import uuid
from collections import OrderedDict
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import texts as t
from api.hikka_client import HikkaClient, LIBRARY_SYNC_INTERVAL
from database.connection import db
from database.queries import SELECT_RECENT_ANIME_SLUGS
from utils.anime_card import (
    prepare_anime_card,
    send_card_to_message,
    send_card_via_callback,
)
from utils.callbacks import AnimeCB, RecentCB

router = Router()

# recent_id -> list[slug]. Список нових тайтлів на момент виклику /new.
# Якщо кеш втрачено (рестарт бота) — fallback пере-опитує БД.
_RECENT_CACHE_MAX = 500
_recent_slugs_cache: "OrderedDict[str, list[str]]" = OrderedDict()


def _set_recent_slugs(recent_id: str, slugs: list[str]) -> None:
    if recent_id in _recent_slugs_cache:
        _recent_slugs_cache.move_to_end(recent_id)
    _recent_slugs_cache[recent_id] = slugs
    while len(_recent_slugs_cache) > _RECENT_CACHE_MAX:
        _recent_slugs_cache.popitem(last=False)


def _get_recent_slugs(recent_id: str) -> Optional[list[str]]:
    return _recent_slugs_cache.get(recent_id)


# cb_id -> (recent_id, index, total). Аналог _notify_ctx_cache —
# дозволяє відновити recent-клавіатуру після "Де дивитись" → "Назад".
_RECENT_CTX_CACHE_MAX = 500
_recent_ctx_cache: "OrderedDict[str, tuple[str, int, int]]" = OrderedDict()


def _set_recent_ctx(cb_id: str, recent_id: str, index: int, total: int) -> None:
    if cb_id in _recent_ctx_cache:
        _recent_ctx_cache.move_to_end(cb_id)
    _recent_ctx_cache[cb_id] = (recent_id, index, total)
    while len(_recent_ctx_cache) > _RECENT_CTX_CACHE_MAX:
        _recent_ctx_cache.popitem(last=False)


def try_get_recent_kb(cb_id: str) -> InlineKeyboardMarkup | None:
    """Повертає recent-клавіатуру для cb_id, якщо ця картка була відрендерена
    у /new-флоу. Інакше None."""
    ctx = _recent_ctx_cache.get(cb_id)
    if ctx is None:
        return None
    recent_id, index, total = ctx
    return _kb_with_nav(cb_id, recent_id, index, total)


def _kb_with_nav(cb_id: str, recent_id: str, index: int, total: int) -> InlineKeyboardMarkup:
    """Клавіатура картки /new: Де дивитись / В улюблені + пагінація.
    Без random-кнопок (BTN_MORE / 'до рекомендацій'), щоб юзер не випадав
    з пачки нових тайтлів у рандом-флоу. На крайніх позиціях prev/next
    лишаються видимими, але стають noop (не реагують на натискання)."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=t.BTN_WHERE_TO_WATCH,
            callback_data=AnimeCB(action="watch", id=cb_id).pack(),
        )],
        [InlineKeyboardButton(
            text=t.BTN_ADD_FAVORITE,
            callback_data=AnimeCB(action="like", id=cb_id).pack(),
        )],
    ]

    prev_cb = (
        RecentCB(action="prev", recent_id=recent_id, index=index - 1).pack()
        if index > 0 else "noop:recent_prev"
    )
    next_cb = (
        RecentCB(action="next", recent_id=recent_id, index=index + 1).pack()
        if index < total - 1 else "noop:recent_next"
    )

    rows.append([
        InlineKeyboardButton(text=t.BTN_RECENT_PREV, callback_data=prev_cb),
        InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="noop:recent_counter"),
        InlineKeyboardButton(text=t.BTN_RECENT_NEXT, callback_data=next_cb),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fetch_recent_slugs() -> list[str]:
    """SELECT slugs новеньких аніме (inserted_at у поточному циклі)."""
    threshold = int(time.time()) - (LIBRARY_SYNC_INTERVAL - 86400)
    rows = db().execute(SELECT_RECENT_ANIME_SLUGS, (threshold,)).fetchall()
    return [row[0] for row in rows]


@router.message(Command("new"))
async def cmd_new(message: Message, db_funcs: dict, hikka_client: HikkaClient):
    slugs = _fetch_recent_slugs()
    if not slugs:
        await message.answer(t.RECENT_EMPTY)
        return

    recent_id = uuid.uuid4().hex[:12]
    _set_recent_slugs(recent_id, slugs)

    index = 0
    slug = slugs[index]
    prepared = await prepare_anime_card(slug, db_funcs, hikka_client)
    if prepared is None:
        # Слаг є у списку але запис зник — рідкісний race; беремо наступний.
        for i in range(1, len(slugs)):
            prepared = await prepare_anime_card(slugs[i], db_funcs, hikka_client)
            if prepared is not None:
                index = i
                slug = slugs[i]
                break
        if prepared is None:
            await message.answer(t.RECENT_EMPTY)
            return

    anime, cb_id, caption = prepared
    kb = _kb_with_nav(cb_id, recent_id, index, len(slugs))
    _set_recent_ctx(cb_id, recent_id, index, len(slugs))

    await send_card_to_message(message, anime, slug, caption, kb)


@router.callback_query(RecentCB.filter(F.action.in_({"next", "prev"})))
async def cb_recent_show(
    callback: CallbackQuery,
    callback_data: RecentCB,
    db_funcs: dict,
    hikka_client: HikkaClient,
):
    slugs = _get_recent_slugs(callback_data.recent_id)
    if slugs is None:
        # Кеш втрачено (рестарт бота) — пере-опитуємо БД.
        slugs = _fetch_recent_slugs()
        if not slugs:
            await callback.answer(t.ALERT_RECENT_GONE, show_alert=True)
            return
        # Перевикористовуємо той самий recent_id, щоб не плодити записи.
        _set_recent_slugs(callback_data.recent_id, slugs)

    index = max(0, min(callback_data.index, len(slugs) - 1))
    slug = slugs[index]

    prepared = await prepare_anime_card(slug, db_funcs, hikka_client)
    if prepared is None:
        await callback.answer(t.ALERT_RECENT_GONE, show_alert=True)
        return

    anime, cb_id, caption = prepared
    kb = _kb_with_nav(cb_id, callback_data.recent_id, index, len(slugs))
    _set_recent_ctx(cb_id, callback_data.recent_id, index, len(slugs))

    await callback.answer()
    await send_card_via_callback(callback, anime, slug, caption, kb)
