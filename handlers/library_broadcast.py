"""Сповіщення користувачів про оновлення бібліотеки.

Після успішного `sync_library`, якщо було додано хоча б один новий тайтл,
розсилаємо всім користувачам, що не вимкнули сповіщення, повідомлення
з кнопками «Перегляд» (відкриває /new) і «Не сповіщати про оновлення»
(показує діалог підтвердження).

Прапорець підписки зберігається в `bot_users.notifications_enabled`
(BOOLEAN DEFAULT TRUE) — міграція в `handlers/admin_panel.py:init_admin_db`.
"""
import asyncio
from typing import Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import texts as t
from api.hikka_client import HikkaClient
from database.connection import db, transaction
from utils.callbacks import MenuCB, NotifCB
from utils.safe_edit import safe_edit_text

router = Router()

_BROADCAST_DELAY_SEC = 0.05  # ~20 msg/sec, нижче ліміту Telegram 30/sec


# ============================================================
# DB helpers
# ============================================================

def get_notifiable_user_ids() -> list[int]:
    """user_id всіх юзерів, що не вимкнули сповіщення (NULL = увімкнено)."""
    rows = db().execute(
        "SELECT user_id FROM bot_users WHERE notifications_enabled IS NOT FALSE"
    ).fetchall()
    return [int(row[0]) for row in rows]


def set_notifications_enabled(user_id: int, enabled: bool) -> None:
    with transaction():
        db().execute(
            "UPDATE bot_users SET notifications_enabled=%s WHERE user_id=%s",
            (enabled, user_id),
        )


def get_notifications_enabled(user_id: int) -> bool:
    row = db().execute(
        "SELECT notifications_enabled FROM bot_users WHERE user_id=%s",
        (user_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return True
    return bool(row[0])


# ============================================================
# Inline keyboards
# ============================================================

def kb_library_update_notif(count: int) -> InlineKeyboardMarkup:
    """Клавіатура під текстом сповіщення про оновлення бази."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t.BTN_NOTIF_OPEN_NEW,
            callback_data=NotifCB(action="open", count=count).pack(),
        )],
        [InlineKeyboardButton(
            text=t.BTN_NOTIF_DISABLE,
            callback_data=NotifCB(action="disable_ask", count=count).pack(),
        )],
    ])


def kb_notif_disable_confirm(count: int) -> InlineKeyboardMarkup:
    """Діалог підтвердження вимкнення сповіщень. count=0 → потік із Налаштувань."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t.BTN_YES,
                callback_data=NotifCB(action="disable_yes", count=count).pack(),
            ),
            InlineKeyboardButton(
                text=t.BTN_NO,
                callback_data=NotifCB(action="disable_no", count=count).pack(),
            ),
        ],
    ])


# ============================================================
# Broadcast
# ============================================================

async def broadcast_library_update(bot: Bot, new_count: int) -> Tuple[int, int]:
    """Розсилає всім підписаним юзерам сповіщення про N нових аніме.
    Повертає (success, failed)."""
    user_ids = get_notifiable_user_ids()
    if not user_ids:
        print("[BROADCAST] Немає юзерів з увімкненими сповіщеннями")
        return (0, 0)

    text = t.NOTIF_LIBRARY_UPDATE_TEXT.format(count=new_count)
    kb = kb_library_update_notif(new_count)
    total = len(user_ids)
    print(f"[BROADCAST] Старт розсилки сповіщення про {new_count} нових тайтлів на {total} юзерів...")

    success = 0
    failed = 0

    for i, uid in enumerate(user_ids, 1):
        try:
            await bot.send_message(uid, text, reply_markup=kb)
            success += 1
        except TelegramForbiddenError:
            # Юзер заблокував бота — мовчки пропускаємо
            failed += 1
        except TelegramBadRequest as e:
            failed += 1
            print(f"[BROADCAST] BadRequest для {uid}: {e}")
        except Exception as e:
            failed += 1
            print(f"[BROADCAST] ⚠️ Помилка для {uid}: {e}")

        if i % 100 == 0:
            print(f"[BROADCAST] Прогрес: {i}/{total} (успіх: {success}, помилок: {failed})")

        await asyncio.sleep(_BROADCAST_DELAY_SEC)

    print(f"[BROADCAST] ✅ Завершено: {success} успіх, {failed} помилок")
    return (success, failed)


# ============================================================
# Callback handlers
# ============================================================

@router.callback_query(NotifCB.filter(F.action == "open"))
async def cb_notif_open(
    callback: CallbackQuery,
    callback_data: NotifCB,
    db_funcs: dict,
    hikka_client: HikkaClient,
):
    """Кнопка «Перегляд» — показує те ж саме, що /new (через спільний helper)."""
    from handlers.recent import open_recent
    await open_recent(callback, db_funcs, hikka_client)


@router.callback_query(NotifCB.filter(F.action == "disable_ask"))
async def cb_notif_disable_ask(callback: CallbackQuery, callback_data: NotifCB):
    """Показує діалог підтвердження вимкнення сповіщень."""
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            t.NOTIF_DISABLE_CONFIRM_TEXT,
            reply_markup=kb_notif_disable_confirm(callback_data.count),
        )


@router.callback_query(NotifCB.filter(F.action == "disable_yes"))
async def cb_notif_disable_yes(callback: CallbackQuery, callback_data: NotifCB):
    set_notifications_enabled(callback.from_user.id, False)
    await callback.answer()
    if callback.message:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t.BTN_BACK, callback_data=MenuCB(action="back").pack())],
        ])
        await safe_edit_text(callback.message, t.NOTIF_DISABLED_DONE, reply_markup=kb)


@router.callback_query(NotifCB.filter(F.action == "disable_no"))
async def cb_notif_disable_no(callback: CallbackQuery, callback_data: NotifCB):
    """Відмова від вимкнення.
    Якщо count > 0 → потік із broadcast, повертаємо оригінальне сповіщення.
    Якщо count == 0 → потік із Settings, повертаємо в меню Налаштувань."""
    await callback.answer()
    if not callback.message:
        return

    if callback_data.count > 0:
        await safe_edit_text(
            callback.message,
            t.NOTIF_LIBRARY_UPDATE_TEXT.format(count=callback_data.count),
            reply_markup=kb_library_update_notif(callback_data.count),
        )
    else:
        # Повертаємось у меню Налаштувань (імпорт всередині — щоб уникнути циклу).
        from UaAnimeRcmd import kb_settings
        await safe_edit_text(
            callback.message,
            t.SETTINGS_TEXT,
            reply_markup=kb_settings(callback.from_user.id),
        )


@router.callback_query(NotifCB.filter(F.action == "enable"))
async def cb_notif_enable(callback: CallbackQuery, callback_data: NotifCB):
    """Увімкнення сповіщень з меню Налаштувань."""
    set_notifications_enabled(callback.from_user.id, True)
    await callback.answer(t.NOTIF_ENABLED_DONE, show_alert=False)
    if callback.message:
        from UaAnimeRcmd import kb_settings
        await safe_edit_text(
            callback.message,
            t.SETTINGS_TEXT,
            reply_markup=kb_settings(callback.from_user.id),
        )
