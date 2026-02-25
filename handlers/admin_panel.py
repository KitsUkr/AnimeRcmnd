import os
import time
import re
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.connection import db
from utils.callbacks import AdminCB, AdCampaignCB
from utils.safe_edit import safe_edit_text

router = Router()

# Кешуємо список адмін-ID одразу при завантаженні модуля
_ADMIN_IDS: set[int] = set()

def _load_admin_ids() -> set[int]:
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    out: set[int] = set()
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out

_ADMIN_IDS = _load_admin_ids()

def is_admin(user_id: int) -> bool:
    return user_id in _ADMIN_IDS

def init_admin_db() -> None:
    from database.connection import transaction
    with transaction():
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
        # Додаємо колонку ad_source якщо її ще немає
        conn.execute(
            """
            ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS ad_source TEXT
            """
        )
        # Таблиця рекламних кампаній
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_campaigns (
                campaign_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )

def touch_user(user_id: int, username: str | None = None, first_name: str | None = None, ad_source: str | None = None) -> None:
    from database.connection import transaction
    now = int(time.time())
    with transaction():
        conn = db()
        if ad_source:
            # Новий юзер з реклами — зберігаємо джерело (не перезаписуємо якщо вже є)
            conn.execute(
                """
                INSERT INTO bot_users(user_id, first_seen_at, last_seen_at, username, first_name, ad_source)
                VALUES(%s, %s, %s, %s, %s, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen_at=EXCLUDED.last_seen_at,
                    username=COALESCE(EXCLUDED.username, bot_users.username),
                    first_name=COALESCE(EXCLUDED.first_name, bot_users.first_name)
                """,
                (user_id, now, now, username, first_name, ad_source),
            )
        else:
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



# ==================
# Ad Campaign CRUD
# ==================

def create_campaign(campaign_id: str, label: str) -> None:
    from database.connection import transaction
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO ad_campaigns(campaign_id, label, created_at)
            VALUES(%s, %s, %s)
            ON CONFLICT(campaign_id) DO UPDATE SET
                label=EXCLUDED.label
            """,
            (campaign_id, label, now),
        )

def delete_campaign(campaign_id: str) -> None:
    from database.connection import transaction
    with transaction():
        conn = db()
        conn.execute("DELETE FROM ad_campaigns WHERE campaign_id = %s", (campaign_id,))

def get_campaigns() -> list[dict]:
    conn = db()
    rows = conn.execute(
        "SELECT campaign_id, label, created_at FROM ad_campaigns ORDER BY created_at DESC"
    ).fetchall()
    return [{"campaign_id": r[0], "label": r[1], "created_at": r[2]} for r in rows]

def get_ad_stats() -> list[dict]:
    """Статистика по кожній кампанії: всього, за 24г, за 7д."""
    now = int(time.time())
    day = 24 * 3600
    week = 7 * day

    conn = db()
    rows = conn.execute(
        """
        SELECT 
            c.campaign_id,
            c.label,
            COALESCE(total.cnt, 0) AS total_users,
            COALESCE(day_cnt.cnt, 0) AS users_24h,
            COALESCE(week_cnt.cnt, 0) AS users_7d
        FROM ad_campaigns c
        LEFT JOIN (
            SELECT ad_source, COUNT(*) AS cnt 
            FROM bot_users WHERE ad_source IS NOT NULL 
            GROUP BY ad_source
        ) total ON total.ad_source = c.campaign_id
        LEFT JOIN (
            SELECT ad_source, COUNT(*) AS cnt 
            FROM bot_users WHERE ad_source IS NOT NULL AND first_seen_at >= %s 
            GROUP BY ad_source
        ) day_cnt ON day_cnt.ad_source = c.campaign_id
        LEFT JOIN (
            SELECT ad_source, COUNT(*) AS cnt 
            FROM bot_users WHERE ad_source IS NOT NULL AND first_seen_at >= %s 
            GROUP BY ad_source
        ) week_cnt ON week_cnt.ad_source = c.campaign_id
        ORDER BY c.created_at DESC
        """,
        (now - day, now - week),
    ).fetchall()

    return [
        {
            "campaign_id": r[0],
            "label": r[1],
            "total_users": int(r[2]),
            "users_24h": int(r[3]),
            "users_7d": int(r[4]),
        }
        for r in rows
    ]

def get_total_ad_users() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) FROM bot_users WHERE ad_source IS NOT NULL").fetchone()
    return int(row[0]) if row else 0


# ==================
# Admin Stats
# ==================

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

    ad_users_total = get_total_ad_users()

    return {
        "total_users": int(total_users),
        "active_24h": int(active_24h),
        "active_7d": int(active_7d),
        "recs_total": int(recs_total),
        "likes_total": int(likes_total),
        "uniq_titles_seen": int(uniq_titles_seen),
        "total_in_base": total_in_base,
        "ad_users_total": ad_users_total,
    }


def _format_admin_text(s: dict) -> str:
    text = (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Учасників (всього): <b>{s['total_users']}</b>\n"
        f"✅ Активних за 24год: <b>{s['active_24h']}</b>\n"
        f"✅ Активних за 7д: <b>{s['active_7d']}</b>\n\n"
        f"🎲 Рекомендацій видано: <b>{s['recs_total']}</b>\n"
        f"📺 Унікальних тайтлів показували: <b>{s['uniq_titles_seen']}</b>\n\n"
        f"👍 Лайків: <b>{s['likes_total']}</b>\n\n"
        f"📊 Аніме в базі: <b>{s['total_in_base'] if s['total_in_base'] is not None else '—'}</b>\n"
        f"📢 Прийшли з реклами: <b>{s['ad_users_total']}</b>\n"
    )
    return text


def kb_admin_panel() -> InlineKeyboardMarkup:
    """Клавіатура для адмін-панелі"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Оновити статистику", callback_data=AdminCB(action="refresh_stats").pack())],
            [InlineKeyboardButton(text="📢 Рекламні кампанії", callback_data=AdCampaignCB(action="list").pack())],
            [InlineKeyboardButton(text="🔄 Синхронізувати бібліотеку", callback_data=AdminCB(action="force_sync").pack())],
        ]
    )


# ==================
# FSM для створення кампанії
# ==================

class AddCampaignStates(StatesGroup):
    waiting_for_id = State()
    waiting_for_label = State()


# ==================
# Admin Panel Handlers
# ==================

@router.message(Command("admin"))
async def admin_cmd(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return

    s = _get_admin_stats()
    text = _format_admin_text(s)
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
    text = _format_admin_text(s)
    
    await safe_edit_text(c.message, text, reply_markup=kb_admin_panel())
    await c.answer("Статистика оновлена ✅", show_alert=False)


# ==================
# Ad Campaign Handlers
# ==================

@router.callback_query(AdCampaignCB.filter(F.action == "list"))
async def cb_ad_campaigns_list(c: CallbackQuery):
    """Список рекламних кампаній"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer("Доступ заборонено", show_alert=True)
        return

    await c.answer()
    stats = get_ad_stats()

    if not stats:
        text = "📢 <b>Рекламні кампанії</b>\n\nПоки що немає жодної кампанії."
    else:
        text = "📢 <b>Рекламні кампанії</b>\n\n"
        for s in stats:
            text += (
                f"▫️ <b>{s['label']}</b> (<code>{s['campaign_id']}</code>)\n"
                f"   Всього: <b>{s['total_users']}</b> · "
                f"24г: <b>{s['users_24h']}</b> · "
                f"7д: <b>{s['users_7d']}</b>\n\n"
            )

    # Кнопки кампаній + створити нову
    buttons = []
    for s in stats:
        buttons.append([
            InlineKeyboardButton(
                text=f"📊 {s['label']}", 
                callback_data=AdCampaignCB(action="detail", campaign_id=s["campaign_id"]).pack()
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="➕ Додати кампанію", 
            callback_data=AdCampaignCB(action="create").pack()
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            text="« Назад", 
            callback_data=AdminCB(action="refresh_stats").pack()
        )
    ])

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "detail"))
async def cb_ad_campaign_detail(c: CallbackQuery, callback_data: AdCampaignCB):
    """Деталі кампанії + deep link"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer("Доступ заборонено", show_alert=True)
        return

    await c.answer()
    campaign_id = callback_data.campaign_id

    # Знаходимо кампанію в статистиці
    stats = get_ad_stats()
    campaign = None
    for s in stats:
        if s["campaign_id"] == campaign_id:
            campaign = s
            break

    if not campaign:
        await c.answer("Кампанію не знайдено", show_alert=True)
        return

    # Отримуємо username бота
    bot: Bot = c.bot
    me = await bot.get_me()
    bot_username = me.username

    deep_link = f"https://t.me/{bot_username}?start=ad_{campaign_id}"

    text = (
        f"📢 <b>{campaign['label']}</b>\n\n"
        f"🆔 ID: <code>{campaign_id}</code>\n\n"
        f"👥 Всього користувачів: <b>{campaign['total_users']}</b>\n"
        f"📈 За 24 години: <b>{campaign['users_24h']}</b>\n"
        f"📈 За 7 днів: <b>{campaign['users_7d']}</b>\n\n"
        f"🔗 <b>Deep link для реклами:</b>\n"
        f"<code>{deep_link}</code>\n"
    )

    buttons = [
        [InlineKeyboardButton(text="🗑 Видалити кампанію", callback_data=AdCampaignCB(action="confirm_delete", campaign_id=campaign_id).pack())],
        [InlineKeyboardButton(text="« Назад до списку", callback_data=AdCampaignCB(action="list").pack())],
    ]

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "confirm_delete"))
async def cb_ad_campaign_confirm_delete(c: CallbackQuery, callback_data: AdCampaignCB):
    """Підтвердження видалення кампанії"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer("Доступ заборонено", show_alert=True)
        return

    await c.answer()
    campaign_id = callback_data.campaign_id

    text = f"⚠️ Ви впевнені, що хочете видалити кампанію <code>{campaign_id}</code>?\n\n<i>Дані про джерело у користувачів збережуться.</i>"
    
    buttons = [
        [
            InlineKeyboardButton(text="✅ Так, видалити", callback_data=AdCampaignCB(action="delete", campaign_id=campaign_id).pack()),
            InlineKeyboardButton(text="❌ Скасувати", callback_data=AdCampaignCB(action="detail", campaign_id=campaign_id).pack()),
        ],
    ]

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "delete"))
async def cb_ad_campaign_delete(c: CallbackQuery, callback_data: AdCampaignCB):
    """Видалення кампанії"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer("Доступ заборонено", show_alert=True)
        return

    campaign_id = callback_data.campaign_id
    delete_campaign(campaign_id)

    await c.answer(f"Кампанію '{campaign_id}' видалено ✅", show_alert=True)

    # Повертаємось до списку — повторюємо логіку списку
    stats = get_ad_stats()

    if not stats:
        text = "📢 <b>Рекламні кампанії</b>\n\nПоки що немає жодної кампанії."
    else:
        text = "📢 <b>Рекламні кампанії</b>\n\n"
        for s in stats:
            text += (
                f"▫️ <b>{s['label']}</b> (<code>{s['campaign_id']}</code>)\n"
                f"   Всього: <b>{s['total_users']}</b> · "
                f"24г: <b>{s['users_24h']}</b> · "
                f"7д: <b>{s['users_7d']}</b>\n\n"
            )

    buttons = []
    for s in stats:
        buttons.append([
            InlineKeyboardButton(
                text=f"📊 {s['label']}",
                callback_data=AdCampaignCB(action="detail", campaign_id=s["campaign_id"]).pack()
            )
        ])
    buttons.append([InlineKeyboardButton(text="➕ Додати кампанію", callback_data=AdCampaignCB(action="create").pack())])
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data=AdminCB(action="refresh_stats").pack())])

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "create"))
async def cb_ad_campaign_create(c: CallbackQuery, state: FSMContext):
    """Початок створення кампанії — запит ID"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer("Доступ заборонено", show_alert=True)
        return

    await c.answer()

    text = (
        "➕ <b>Нова рекламна кампанія</b>\n\n"
        "Надішліть <b>ID кампанії</b> (латиниця, цифри, підкреслення).\n"
        "Наприклад: <code>instagram_feb</code>\n\n"
        "<i>Або натисніть «Скасувати»</i>"
    )
    buttons = [[InlineKeyboardButton(text="❌ Скасувати", callback_data=AdCampaignCB(action="list").pack())]]

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(AddCampaignStates.waiting_for_id)


@router.message(AddCampaignStates.waiting_for_id)
async def fsm_campaign_id(m: Message, state: FSMContext):
    """Отримали ID кампанії — запитуємо назву"""
    uid = m.from_user.id
    if not is_admin(uid):
        await state.clear()
        return

    campaign_id = m.text.strip()

    # Валідація: тільки латиниця, цифри, _, -
    if not re.match(r"^[a-zA-Z0-9_\-]+$", campaign_id):
        await m.answer(
            "⚠️ ID може містити лише латиницю, цифри, <code>_</code> та <code>-</code>.\n"
            "Спробуйте ще раз:"
        )
        return

    if len(campaign_id) > 50:
        await m.answer("⚠️ ID занадто довгий (макс. 50 символів). Спробуйте ще раз:")
        return

    await state.update_data(campaign_id=campaign_id)
    await state.set_state(AddCampaignStates.waiting_for_label)
    await m.answer(
        f"Добре, ID: <code>{campaign_id}</code>\n\n"
        "Тепер надішліть <b>назву</b> кампанії (довільний текст).\n"
        "Наприклад: <i>Реклама в інстаграм лютий</i>"
    )


@router.message(AddCampaignStates.waiting_for_label)
async def fsm_campaign_label(m: Message, state: FSMContext):
    """Отримали назву — створюємо кампанію"""
    uid = m.from_user.id
    if not is_admin(uid):
        await state.clear()
        return

    label = m.text.strip()
    if not label or len(label) > 100:
        await m.answer("⚠️ Назва не може бути порожньою або довшою за 100 символів. Спробуйте ще:")
        return

    data = await state.get_data()
    campaign_id = data["campaign_id"]

    create_campaign(campaign_id, label)
    await state.clear()

    # Отримуємо deep link
    me = await m.bot.get_me()
    bot_username = me.username
    deep_link = f"https://t.me/{bot_username}?start=ad_{campaign_id}"

    await m.answer(
        f"✅ Кампанію створено!\n\n"
        f"📢 <b>{label}</b>\n"
        f"🆔 ID: <code>{campaign_id}</code>\n\n"
        f"🔗 Deep link:\n<code>{deep_link}</code>\n\n"
        f"Використовуйте /admin щоб переглянути статистику."
    )
