import asyncio

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import texts as t
from api.hikka_client import META_LAST_NOTIFY_BATCH_ID, meta_get, meta_set
from database.connection import db, transaction
from database.queries import (
    SELECT_SYNC_BATCH,
    SELECT_USERS_FOR_BROADCAST,
    UPDATE_SYNC_BATCH_CURSOR,
    UPDATE_USER_NOTIFY_OFF,
)
from utils.callbacks import NotifyCB

SEND_DELAY_SECONDS = 0.05  # ≈20 msg/s, безпечно під лімітом 30
CURSOR_FLUSH_EVERY = 50    # частота оновлення курсора в БД


def _kb_for_notify(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=t.BTN_NOTIFY_SHOW_ADDED,
                callback_data=NotifyCB(action="show", batch_id=batch_id, index=0).pack(),
            )],
            [InlineKeyboardButton(
                text=t.BTN_NOTIFY_OPT_OUT,
                callback_data=NotifyCB(action="off", batch_id=batch_id).pack(),
            )],
        ]
    )


def _disable_user_notify(user_id: int) -> None:
    with transaction():
        db().execute(UPDATE_USER_NOTIFY_OFF, (user_id,))


def _flush_cursor(batch_id: str, last_user_id: int, sent_delta: int) -> None:
    with transaction():
        db().execute(UPDATE_SYNC_BATCH_CURSOR, (last_user_id, sent_delta, batch_id))


async def broadcast_library_update(bot: Bot, batch_id: str) -> None:
    """Розсилає всім юзерам (з notify_updates=TRUE) повідомлення про оновлення
    бібліотеки. Idempotent: повторний виклик з тим самим batch_id — no-op.
    Re-entry safe: тримає курсор по last_user_id у library_sync_batches.
    """
    cached = meta_get(META_LAST_NOTIFY_BATCH_ID)
    if cached and cached[0] == batch_id:
        print(f"[BROADCAST] Пачка {batch_id} вже розіслана, skip")
        return

    row = db().execute(SELECT_SYNC_BATCH, (batch_id,)).fetchone()
    if not row:
        print(f"[BROADCAST] ❌ Пачка {batch_id} не знайдена")
        return
    _slugs_json, items_count, last_user_id = row

    text = t.NOTIFY_LIBRARY_UPDATED.format(count=items_count)
    kb = _kb_for_notify(batch_id)

    users = db().execute(SELECT_USERS_FOR_BROADCAST, (last_user_id,)).fetchall()
    if not users:
        meta_set(META_LAST_NOTIFY_BATCH_ID, batch_id)
        print(f"[BROADCAST] {batch_id}: жодного юзера для розсилки")
        return

    print(f"[BROADCAST] {batch_id}: розсилаю {len(users)} юзерам...")

    sent_since_flush = 0
    sent_total = 0
    failed = 0
    blocked = 0
    last_processed = last_user_id

    for (user_id,) in users:
        try:
            await bot.send_message(
                user_id, text, reply_markup=kb, parse_mode=ParseMode.HTML
            )
            sent_total += 1
            sent_since_flush += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(
                    user_id, text, reply_markup=kb, parse_mode=ParseMode.HTML
                )
                sent_total += 1
                sent_since_flush += 1
            except Exception as e2:
                failed += 1
                print(f"[BROADCAST] retry failed for {user_id}: {e2}")
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            # Юзер заблокував / видалив акаунт / чат не знайдено — м'який opt-out
            blocked += 1
            _disable_user_notify(user_id)
        except Exception as e:
            failed += 1
            print(f"[BROADCAST] send failed for {user_id}: {e}")

        last_processed = user_id

        if sent_since_flush >= CURSOR_FLUSH_EVERY:
            _flush_cursor(batch_id, last_processed, sent_since_flush)
            sent_since_flush = 0

        await asyncio.sleep(SEND_DELAY_SECONDS)

    # Фінальний flush курсора
    if sent_since_flush > 0:
        _flush_cursor(batch_id, last_processed, sent_since_flush)

    meta_set(META_LAST_NOTIFY_BATCH_ID, batch_id)
    print(
        f"[BROADCAST] {batch_id}: завершено. sent={sent_total}, "
        f"blocked={blocked}, failed={failed}"
    )
