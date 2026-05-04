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
import texts as t

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
    text = t.ADMIN_PANEL_TEXT.format(
        total_users=s['total_users'],
        active_24h=s['active_24h'],
        active_7d=s['active_7d'],
        recs_total=s['recs_total'],
        uniq_titles_seen=s['uniq_titles_seen'],
        likes_total=s['likes_total'],
        total_in_base=s['total_in_base'] if s['total_in_base'] is not None else '—',
        ad_users_total=s['ad_users_total'],
    )
    return text


def kb_admin_panel() -> InlineKeyboardMarkup:
    """Клавіатура для адмін-панелі"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.BTN_REFRESH_STATS, callback_data=AdminCB(action="refresh_stats").pack())],
            [InlineKeyboardButton(text=t.BTN_AD_CAMPAIGNS, callback_data=AdCampaignCB(action="list").pack())],
            [InlineKeyboardButton(text=t.BTN_SYNC_LIBRARY, callback_data=AdminCB(action="force_sync").pack())],
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
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
        return
    
    await c.answer(t.ALERT_REFRESHING_STATS)
    
    s = _get_admin_stats()
    text = _format_admin_text(s)
    
    await safe_edit_text(c.message, text, reply_markup=kb_admin_panel())
    await c.answer(t.ALERT_STATS_REFRESHED, show_alert=False)


# ==================
# Ad Campaign Handlers
# ==================

@router.callback_query(AdCampaignCB.filter(F.action == "list"))
async def cb_ad_campaigns_list(c: CallbackQuery):
    """Список рекламних кампаній"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
        return

    await c.answer()
    stats = get_ad_stats()

    if not stats:
        text = t.AD_CAMPAIGNS_EMPTY
    else:
        text = f"{t.AD_CAMPAIGNS_TITLE}\n\n"
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
            text=t.BTN_ADD_CAMPAIGN, 
            callback_data=AdCampaignCB(action="create").pack()
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            text=t.BTN_BACK, 
            callback_data=AdminCB(action="refresh_stats").pack()
        )
    ])

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "detail"))
async def cb_ad_campaign_detail(c: CallbackQuery, callback_data: AdCampaignCB):
    """Деталі кампанії + deep link"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
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
        await c.answer(t.ALERT_CAMPAIGN_NOT_FOUND, show_alert=True)
        return

    # Отримуємо username бота
    bot: Bot = c.bot
    me = await bot.get_me()
    bot_username = me.username

    deep_link = f"https://t.me/{bot_username}?start=ad_{campaign_id}"

    text = t.AD_CAMPAIGN_DETAIL.format(
        label=campaign['label'],
        campaign_id=campaign_id,
        total_users=campaign['total_users'],
        users_24h=campaign['users_24h'],
        users_7d=campaign['users_7d'],
        deep_link=deep_link,
    )

    buttons = [
        [InlineKeyboardButton(text=t.BTN_DELETE_CAMPAIGN, callback_data=AdCampaignCB(action="confirm_delete", campaign_id=campaign_id).pack())],
        [InlineKeyboardButton(text=t.BTN_BACK_TO_LIST, callback_data=AdCampaignCB(action="list").pack())],
    ]

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "confirm_delete"))
async def cb_ad_campaign_confirm_delete(c: CallbackQuery, callback_data: AdCampaignCB):
    """Підтвердження видалення кампанії"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
        return

    await c.answer()
    campaign_id = callback_data.campaign_id

    text = t.AD_CAMPAIGN_CONFIRM_DELETE.format(campaign_id=campaign_id)
    
    buttons = [
        [
            InlineKeyboardButton(text=t.BTN_YES_DELETE, callback_data=AdCampaignCB(action="delete", campaign_id=campaign_id).pack()),
            InlineKeyboardButton(text=t.BTN_CANCEL, callback_data=AdCampaignCB(action="detail", campaign_id=campaign_id).pack()),
        ],
    ]

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "delete"))
async def cb_ad_campaign_delete(c: CallbackQuery, callback_data: AdCampaignCB):
    """Видалення кампанії"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
        return

    campaign_id = callback_data.campaign_id
    delete_campaign(campaign_id)

    await c.answer(t.AD_CAMPAIGN_DELETED.format(campaign_id=campaign_id), show_alert=True)

    # Повертаємось до списку — повторюємо логіку списку
    stats = get_ad_stats()

    if not stats:
        text = t.AD_CAMPAIGNS_EMPTY
    else:
        text = f"{t.AD_CAMPAIGNS_TITLE}\n\n"
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
    buttons.append([InlineKeyboardButton(text=t.BTN_ADD_CAMPAIGN, callback_data=AdCampaignCB(action="create").pack())])
    buttons.append([InlineKeyboardButton(text=t.BTN_BACK, callback_data=AdminCB(action="refresh_stats").pack())])

    await safe_edit_text(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdCampaignCB.filter(F.action == "create"))
async def cb_ad_campaign_create(c: CallbackQuery, state: FSMContext):
    """Початок створення кампанії — запит ID"""
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer(t.ALERT_ACCESS_DENIED, show_alert=True)
        return

    await c.answer()

    text = t.AD_CAMPAIGN_FSM_ASK_ID
    buttons = [[InlineKeyboardButton(text=t.BTN_CANCEL, callback_data=AdCampaignCB(action="list").pack())]]

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
        await m.answer(t.AD_CAMPAIGN_FSM_INVALID_ID)
        return

    if len(campaign_id) > 50:
        await m.answer(t.AD_CAMPAIGN_FSM_ID_TOO_LONG)
        return

    await state.update_data(campaign_id=campaign_id)
    await state.set_state(AddCampaignStates.waiting_for_label)
    await m.answer(t.AD_CAMPAIGN_FSM_ASK_LABEL.format(campaign_id=campaign_id))


@router.message(AddCampaignStates.waiting_for_label)
async def fsm_campaign_label(m: Message, state: FSMContext):
    """Отримали назву — створюємо кампанію"""
    uid = m.from_user.id
    if not is_admin(uid):
        await state.clear()
        return

    label = m.text.strip()
    if not label or len(label) > 100:
        await m.answer(t.AD_CAMPAIGN_FSM_LABEL_INVALID)
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
        t.AD_CAMPAIGN_CREATED.format(
            label=label,
            campaign_id=campaign_id,
            deep_link=deep_link,
        )
    )
