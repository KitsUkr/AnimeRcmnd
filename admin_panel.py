import os
import time
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import db
from callbacks import AdminCB
from safe_edit import safe_edit_text

router = Router()

def _admin_ids() -> set[int]:
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    out: set[int] = set()
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out

def is_admin(user_id: int) -> bool:
    return user_id in _admin_ids()

def init_admin_db() -> None:
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            username TEXT,
            first_name TEXT
        )
        """
    )

def touch_user(user_id: int, username: str | None = None, first_name: str | None = None) -> None:
    now = int(time.time())
    conn = db()
    conn.execute(
        """
        INSERT INTO bot_users(user_id, first_seen_at, last_seen_at, username, first_name)
        VALUES(%s, %s, %s, %s, %s)
        ON CONFLICT(user_id) DO UPDATE SET
            last_seen_at=EXCLUDED.last_seen_at,
            username=COALESCE(EXCLUDED.username, bot_users.username),
            first_name=COALESCE(EXCLUDED.first_name, bot_users.first_name)
        """,
        (user_id, now, now, username, first_name),
    )

def _get_admin_stats() -> dict:
    now = int(time.time())
    day = 24 * 3600
    week = 7 * day

    conn = db()
    total_users = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0] or 0
    active_24h = conn.execute(
        "SELECT COUNT(*) FROM bot_users WHERE last_seen_at >= %s",
        (now - day,),
    ).fetchone()[0] or 0
    active_7d = conn.execute(
        "SELECT COUNT(*) FROM bot_users WHERE last_seen_at >= %s",
        (now - week,),
    ).fetchone()[0] or 0

    recs_total = conn.execute("SELECT COUNT(*) FROM user_seen").fetchone()[0] or 0

    likes_total = conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE value=1"
    ).fetchone()[0] or 0

    uniq_titles_seen = conn.execute(
        "SELECT COUNT(DISTINCT anime_id) FROM user_seen"
    ).fetchone()[0] or 0

    row = conn.execute(
        "SELECT value FROM bot_meta WHERE key=%s",
        ("total_translated_titles",),
    ).fetchone()
    total_in_base = int(row[0]) if row and str(row[0]).isdigit() else None

    return {
        "total_users": int(total_users),
        "active_24h": int(active_24h),
        "active_7d": int(active_7d),
        "recs_total": int(recs_total),
        "likes_total": int(likes_total),
        "uniq_titles_seen": int(uniq_titles_seen),
        "total_in_base": total_in_base,
    }

def kb_admin_panel() -> InlineKeyboardMarkup:
    """Клавіатура для адмін-панелі"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Оновити статистику", callback_data=AdminCB(action="refresh_stats").pack())],
            [InlineKeyboardButton(text="🔄 Синхронізувати бібліотеку", callback_data=AdminCB(action="force_sync").pack())],
        ]
    )

@router.message(Command("admin"))
async def admin_cmd(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return

    s = _get_admin_stats()
    text = (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Учасників (всього): <b>{s['total_users']}</b>\n"
        f"✅ Активних за 24год: <b>{s['active_24h']}</b>\n"
        f"✅ Активних за 7д: <b>{s['active_7d']}</b>\n\n"
        f"🎲 Рекомендацій видано: <b>{s['recs_total']}</b>\n"
        f"📺 Унікальних тайтлів показували: <b>{s['uniq_titles_seen']}</b>\n\n"
        f"👍 Лайків: <b>{s['likes_total']}</b>\n\n"
        f"📊 Аніме в базі: <b>{s['total_in_base'] if s['total_in_base'] is not None else '—'}</b>\n"
    )
    await m.answer(text, reply_markup=kb_admin_panel())

@router.callback_query(AdminCB.filter(F.action == "refresh_stats"))
async def cb_admin_refresh_stats(c: CallbackQuery):
    """Обновлення статистики в адмін-панелі"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer("Доступ заборонено", show_alert=True)
        return
    
    await c.answer("Оновлюю статистику...")
    
    s = _get_admin_stats()
    text = (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Учасників (всього): <b>{s['total_users']}</b>\n"
        f"✅ Активних за 24год: <b>{s['active_24h']}</b>\n"
        f"✅ Активних за 7д: <b>{s['active_7d']}</b>\n\n"
        f"🎲 Рекомендацій видано: <b>{s['recs_total']}</b>\n"
        f"📺 Унікальних тайтлів показували: <b>{s['uniq_titles_seen']}</b>\n\n"
        f"👍 Лайків: <b>{s['likes_total']}</b>\n\n"
        f"📊 Аніме в базі: <b>{s['total_in_base'] if s['total_in_base'] is not None else '—'}</b>\n"
    )
    
    await safe_edit_text(c.message, text, reply_markup=kb_admin_panel())
    await c.answer("Статистика оновлена ✅", show_alert=False)
