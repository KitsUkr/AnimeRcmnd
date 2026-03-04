import html
import time
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from database.connection import db, transaction
import json
from utils.ui_shared import Anime, format_caption, MAX_CAPTION
from aiogram.types import InputMediaPhoto
from utils.callbacks import MenuCB, HikkaCB, SettingsCB
from utils.safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup
from api.hikka_auth import HikkaAuth, get_hikka_token, delete_hikka_token, is_hikka_logged_in

router = Router()

def get_user_stats(user_id: int) -> dict:
    conn = db()
    seen = conn.execute(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=%s",
        (user_id,),
    ).fetchone()[0]

    likes = conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE user_id=%s AND value=1",
        (user_id,),
    ).fetchone()[0] or 0

    return {
        "seen": seen,
        "likes": likes,
    }

def get_liked_titles_count(user_id: int) -> int:
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE user_id=%s AND value=1",
        (user_id,),
    ).fetchone()
    return int(row[0] or 0)

def get_liked_title_at(user_id: int, idx: int) -> tuple[str, str] | None:
    conn = db()
    row = conn.execute(
        """
        SELECT anime_id, COALESCE(title, anime_id) AS title
        FROM user_feedback
        WHERE user_id=%s AND value=1
        ORDER BY ts DESC
        LIMIT 1 OFFSET %s
        """,
        (user_id, int(idx)),
    ).fetchone()
    if not row:
        return None
    return str(row[0]), str(row[1])

def get_liked_count(user_id: int) -> int:
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE user_id=%s AND value=1",
        (user_id,),
    ).fetchone()
    return int(row[0] or 0)

def get_liked_snapshot(user_id: int, idx: int) -> dict | None:
    conn = db()
    row = conn.execute(
        """
        SELECT anime_id, title, poster_url, hikka_url,
               year, score, episodes_total, genres_json,
               description, watch_links_json, ua_poster_url
        FROM user_feedback
        WHERE user_id=%s AND value=1
        ORDER BY ts DESC
        LIMIT 1 OFFSET %s
        """,
        (user_id, int(idx)),
    ).fetchone()

    if not row:
        return None

    (anime_id, title, poster_url, hikka_url,
     year, score, episodes_total, genres_json,
     description, watch_links_json, ua_poster_url) = row

    try:
        genres = json.loads(genres_json) if genres_json else []
        if not isinstance(genres, list):
            genres = []
    except Exception as e:
        print(f"Error loading genres (profile): {e}")
        genres = []

    try:
        watch_links = json.loads(watch_links_json) if watch_links_json else []
        if not isinstance(watch_links, list):
            watch_links = []
    except Exception as e:
        print(f"Error loading watch links (profile): {e}")
        watch_links = []

    return {
        "anime_id": str(anime_id),
        "title": str(title) if title else str(anime_id),
        "poster_url": str(poster_url) if poster_url else None,
        "hikka_url": str(hikka_url) if hikka_url else None,
        "year": int(year) if year is not None else None,
        "score": float(score) if score is not None else None,
        "episodes_total": int(episodes_total) if episodes_total is not None else None,
        "genres": [str(x) for x in genres][:8],
        "description": str(description) if description else None,
        "watch_links": watch_links,
        "ua_poster_url": str(ua_poster_url) if ua_poster_url else None,
    }

def kb_likes_photo(idx: int, total: int, has_watch: bool) -> InlineKeyboardMarkup:
    # Navigation - кнопки завжди показуються, неактивні мають noop
    nav = []

    if total > 1 and idx > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"prof:likes_photo:{idx-1}"))
    else:
        nav.append(InlineKeyboardButton(text="«", callback_data="noop:left"))

    nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop:page"))

    if total > 1 and idx < total - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"prof:likes_photo:{idx+1}"))
    else:
        nav.append(InlineKeyboardButton(text="»", callback_data="noop:right"))

    kb = InlineKeyboardMarkup(inline_keyboard=[])

    if has_watch:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🤔 Де дивитись", callback_data=f"prof:likes_watch:{idx}")
        ])

    kb.inline_keyboard.append(nav)

    kb.inline_keyboard.append([
        InlineKeyboardButton(text="🗑️ Видалити", callback_data=f"prof:unlike:{idx}")
    ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(text="« Назад", callback_data="prof:likes_close")
    ])

    return kb


def clear_likes(user_id: int) -> int:
    with transaction():
        conn = db()
        cur = conn.execute(
            "DELETE FROM user_feedback WHERE user_id=%s AND value=1",
            (user_id,),
        )
        return cur.rowcount


def unlike_title(user_id: int, anime_id: str) -> None:
    with transaction():
        conn = db()
        conn.execute(
            "DELETE FROM user_feedback WHERE user_id=%s AND anime_id=%s AND value=1",
            (user_id, anime_id),
        )


async def send_profile(target: Message, user_id: int, *, edit: bool = False):
    if target.chat.type != "private":
        msg = "👤 Профіль доступний тільки в особистому чаті з ботом."
        if edit:
            await safe_edit_text(target, msg)
        else:
            await target.answer(msg)
        return

    stats = get_user_stats(user_id)
    total = get_total_translated_titles()

    text = (
        "👤 <b>Твій профіль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"🎲 Рекомендацій отримано: <b>{stats['seen']}</b>\n"
        f"📊 Усього аніме в базі: <b>{total}</b>\n\n"
        f"❤️ Додано в обрані: <b>{stats['likes']}</b>\n"
    )

    if edit:
        if target.content_type == "photo":
            try:
                await target.delete()
            except Exception:
                pass
            await target.answer(text, reply_markup=kb_profile())
        else:
            await safe_edit_text(target, text, reply_markup=kb_profile())
    else:
        await target.answer(text, reply_markup=kb_profile())

def kb_profile(user_id: int = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="❤️ Обрані", callback_data="prof:likes_open:0")],
        [InlineKeyboardButton(text="📜 Історія рекомендацій", callback_data=MenuCB(action="history").pack())],
        [InlineKeyboardButton(text="🧹 Очистити", callback_data="prof:clear_menu")],
        [InlineKeyboardButton(text="« Назад", callback_data="start:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_profile_clear_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❤️ Очистити обрані", callback_data="prof:clear_likes")],
            [InlineKeyboardButton(text="📜 Очистити історію", callback_data="prof:clear_history")],
            [InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")],
        ]
    )

def kb_likes_pager(idx: int, total: int) -> InlineKeyboardMarkup:
    # Navigation - кнопки завжди показуються, неактивні мають noop
    row = []
    if total > 1 and idx > 0:
        row.append(InlineKeyboardButton(text="«", callback_data=f"prof:likes:{idx-1}"))
    else:
        row.append(InlineKeyboardButton(text="«", callback_data="noop:left"))
    
    row.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop:page"))
    
    if total > 1 and idx < total - 1:
        row.append(InlineKeyboardButton(text="»", callback_data=f"prof:likes:{idx+1}"))
    else:
        row.append(InlineKeyboardButton(text="»", callback_data="noop:right"))

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    kb.inline_keyboard.append(row)

    kb.inline_keyboard.append([InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")])
    return kb

def kb_likes_view(idx: int, total: int) -> InlineKeyboardMarkup:
    # Navigation - кнопки завжди показуються, неактивні мають noop
    nav = []
    if total > 1 and idx > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"prof:likes:{idx-1}"))
    else:
        nav.append(InlineKeyboardButton(text="«", callback_data="noop:left"))
    
    nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop:page"))
    
    if total > 1 and idx < total - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"prof:likes:{idx+1}"))
    else:
        nav.append(InlineKeyboardButton(text="»", callback_data="noop:right"))

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    kb.inline_keyboard.append(nav)

    kb.inline_keyboard.append([InlineKeyboardButton(text="🤔 Де дивитись", callback_data=f"prof:likes_watch:{idx}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")])
    return kb

def render_like_card(s: dict) -> str:
    from handlers.filters.content import get_name_by_slug
    year = f" ({s['year']})" if s.get("year") else ""
    score = f"⭐ <b>{s['score']:.1f}</b>" if isinstance(s.get("score"), float) else ""
    eps = f" · <b>{s['episodes_total']}</b> еп." if s.get("episodes_total") else ""
    ctype = f" · {get_name_by_slug(s['content_type'])}" if s.get("content_type") else ""
    genres = f"\nЖанри: <i>{', '.join(s['genres'])}</i>" if s.get("genres") else ""
    link_line = f"\n\n🔎 <a href=\"{html.escape(s['hikka_url'])}\">Сторінка на Hikka</a>" if s.get("hikka_url") else ""

    base = (
        f"❤️ <b>Обрані тайтли</b>\n\n"
        f"<b>{html.escape(s['title'])}</b>{year}\n"
        f"{score}{eps}{ctype}"
        f"{genres}"
    )

    desc = s.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return base + link_line

    synopsis = html.escape(desc.strip().replace("\n", " "))
    return base + f"\n\n<i>📖 Синопсис:</i> {synopsis}" + link_line


async def show_liked(target: Message, user_id: int, idx: int):
    total = get_liked_count(user_id)
    if total <= 0:
        await safe_edit_text(target,
            "❤️ <b>Обрані тайтли</b>\n\nСписок обраних пуст 💔",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")]]
            ),
        )
        return

    idx = max(0, min(idx, total - 1))
    snap = get_liked_snapshot(user_id, idx)
    if not snap:
        await safe_edit_text(target,
            "❤️ <b>Обрані тайтли</b>\n\nНе вдалося завантажити.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")]]
            ),
        )
        return

    await safe_edit_text(target, render_like_card(snap), reply_markup=kb_likes_view(idx, total))

def kb_likes_watch(idx: int, watch_links: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    # Розділяємо на звичайні сайти та торренти (Toloka)
    regular_links = []
    has_toloka = False
    
    for link in watch_links[:20]:
        text_btn = str(link.get("text") or link.get("url") or "").strip()
        if text_btn.lower() == "toloka":
            has_toloka = True
        else:
            regular_links.append(link)

    if not regular_links and has_toloka:
        # Тільки торрент-посилання — показуємо Толоку напряму без доп. кнопки
        for link in watch_links[:20]:
            text_btn = str(link.get("text") or link.get("url") or "").strip()
            url = str(link.get("url") or "").strip()
            if not url:
                continue
            if text_btn.lower() != "toloka":
                continue
            if len(text_btn) > 60:
                text_btn = text_btn[:57] + "..."
            kb.inline_keyboard.append([InlineKeyboardButton(text=f"• {text_btn}", url=url)])
    else:
        # Звичайні сайти
        for link in regular_links:
            text_btn = str(link.get("text") or link.get("url") or "").strip()
            url = str(link.get("url") or "").strip()
            if not url:
                continue
            if len(text_btn) > 60:
                text_btn = text_btn[:57] + "..."
            kb.inline_keyboard.append([InlineKeyboardButton(text=f"• {text_btn}", url=url)])

        # Кнопка торрентів якщо є і звичайні, і Толока
        if has_toloka:
            kb.inline_keyboard.append([InlineKeyboardButton(text="📥 Торренти", callback_data=f"prof:likes_torrents:{idx}")])

    kb.inline_keyboard.append([InlineKeyboardButton(text="« Назад", callback_data=f"prof:likes_photo:{idx}")])
    return kb


def kb_likes_torrents(idx: int, watch_links: list[dict]) -> InlineKeyboardMarkup:
    """Клавіатура для торрент-посилань (Толока) в обраних"""
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    # Toloka links
    for link in watch_links[:20]:
        text_btn = str(link.get("text") or link.get("url") or "").strip()
        url = str(link.get("url") or "").strip()
        if not url:
            continue
        if text_btn.lower() != "toloka":
            continue
        if len(text_btn) > 60:
            text_btn = text_btn[:57] + "..."
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"• {text_btn}", url=url)])

    kb.inline_keyboard.append([InlineKeyboardButton(text="« Назад", callback_data=f"prof:likes_watch:{idx}")])
    return kb

async def open_likes_viewer(c: CallbackQuery, idx: int):
    user_id = c.from_user.id
    total = get_liked_count(user_id)

    if total <= 0:
        await c.answer("Список обраних пуст 💔", show_alert=True)
        return

    idx = max(0, min(idx, total - 1))
    snap = get_liked_snapshot(user_id, idx)
    if not snap:
        await c.answer("Не вдалося завантажити.", show_alert=True)
        return

    try:
        if c.message:
            await c.message.delete()
    except Exception:
        pass

    a = Anime(
        id=snap["anime_id"],
        slug=snap["anime_id"],
        title=snap["title"],
        year=snap.get("year"),
        score=snap.get("score"),
        genres=snap.get("genres") or [],
        episodes_total=snap.get("episodes_total"),
        description=snap.get("description"),
        poster_url=snap.get("poster_url"),
        hikka_url=snap.get("hikka_url"),
        watch_links=snap.get("watch_links") or [],
        ua_poster_url=snap.get("ua_poster_url"),
    )
    caption = format_caption(a)
    final_poster = a.ua_poster_url or a.poster_url

    if final_poster:
        await c.bot.send_photo(
            chat_id=c.from_user.id,
            photo=final_poster,
            caption=caption,
            reply_markup=kb_likes_photo(
            idx,
            total,
            has_watch=bool(a.watch_links),
            ),
        )
    else:
        await c.bot.send_message(
            chat_id=c.from_user.id,
            text=caption,
            reply_markup=kb_likes_photo(idx, total),
        )

async def show_liked_title(target: Message, user_id: int, idx: int) -> None:
    total = get_liked_titles_count(user_id)
    if total <= 0:
        await safe_edit_text(target,
            "❤️ <b>Обрані тайтли</b>\n\nСписок обраних пуст 💔",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")]]
            ),
        )
        return

    idx = max(0, min(idx, total - 1))

    row = get_liked_title_at(user_id, idx)
    if not row:
        await safe_edit_text(target,
            "❤️ <b>Обрані тайтли</b>\n\nНе вдалося завантажити список.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="prof:back_to_profile")]]
            ),
        )
        return

    anime_id, title = row
    text = (
        "❤️ <b>Обрані тайтли</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"<code>{html.escape(anime_id)}</code>"
    )

    await safe_edit_text(target, text, reply_markup=kb_likes_pager(idx, total))


@router.callback_query(F.data.startswith("prof:unlike:"))
async def prof_unlike(c: CallbackQuery):
    if not c.message:
        await c.answer()
        return

    user_id = c.from_user.id
    total_before = get_liked_count(user_id)
    if total_before <= 0:
        await c.answer("Список обраних пуст 💔", show_alert=True)
        return

    try:
        idx = int(c.data.split(":")[-1])
    except Exception:
        idx = 0
    idx = max(0, min(idx, total_before - 1))

    snap = get_liked_snapshot(user_id, idx)
    if not snap:
        await c.answer("Не вдалося завантажити.", show_alert=True)
        return

    unlike_title(user_id, snap["anime_id"])

    total_after = get_liked_count(user_id)
    if total_after <= 0:
        try:
            if c.message:
                await c.message.delete()
        except Exception:
            pass

        await c.answer("Це було останнє аніме з вашого списку", show_alert=True)

        chat_id = c.from_user.id
        m = await c.bot.send_message(chat_id, "👤 Відкриваю профіль…")
        await send_profile(m, c.from_user.id, edit=True)
        return

    await c.answer("Прибрано з обраних ✅")

    # Update the view with the next item
    new_idx = min(idx, total_after - 1)

    new_snap = get_liked_snapshot(user_id, new_idx)
    if not new_snap:
        await c.answer("Не вдалося оновити список.", show_alert=True)
        return

    a = Anime(
        id=new_snap["anime_id"],
        slug=new_snap["anime_id"],
        title=new_snap["title"],
        year=new_snap.get("year"),
        score=new_snap.get("score"),
        genres=new_snap.get("genres") or [],
        episodes_total=new_snap.get("episodes_total"),
        description=new_snap.get("description"),
        poster_url=new_snap.get("poster_url"),
        hikka_url=new_snap.get("hikka_url"),
        watch_links=new_snap.get("watch_links") or [],
        ua_poster_url=new_snap.get("ua_poster_url"),
    )
    caption = format_caption(a)
    markup = kb_likes_photo(new_idx, total_after, has_watch=bool(a.watch_links))
    final_poster = a.ua_poster_url or a.poster_url

    if final_poster:
        await safe_edit_media(c.message, InputMediaPhoto(media=final_poster, caption=caption), reply_markup=markup)
    else:
        await safe_edit_text(c.message, caption, reply_markup=markup)


@router.callback_query(F.data.startswith("prof:likes_photo:"))
async def prof_likes_photo_pager(c: CallbackQuery):
    if not c.message:
        await c.answer()
        return

    user_id = c.from_user.id
    total = get_liked_count(user_id)
    if total <= 0:
        await c.answer("Список обраних пуст 💔", show_alert=True)
        return

    try:
        idx = int(c.data.split(":")[-1])
    except Exception:
        idx = 0
    idx = max(0, min(idx, total - 1))

    snap = get_liked_snapshot(user_id, idx)
    if not snap:
        await c.answer("Не вдалося завантажити.", show_alert=True)
        return

    a = Anime(
        id=snap["anime_id"],
        slug=snap["anime_id"],
        title=snap["title"],
        year=snap.get("year"),
        score=snap.get("score"),
        genres=snap.get("genres") or [],
        episodes_total=snap.get("episodes_total"),
        description=snap.get("description"),
        poster_url=snap.get("poster_url"),
        hikka_url=snap.get("hikka_url"),
        watch_links=snap.get("watch_links") or [],
        ua_poster_url=snap.get("ua_poster_url"),
    )
    caption = format_caption(a)
    markup = kb_likes_photo(idx, total, has_watch=bool(a.watch_links))
    final_poster = a.ua_poster_url or a.poster_url

    await c.answer()

    if final_poster:
        await safe_edit_media(c.message, InputMediaPhoto(media=final_poster, caption=caption), reply_markup=markup)
    else:
        await safe_edit_text(c.message, caption, reply_markup=markup)

@router.callback_query(F.data == "prof:likes_close")
async def prof_likes_close(c: CallbackQuery):
    await c.answer()
    try:
        if c.message:
            await c.message.delete()
    except Exception:
        pass

    chat_id = c.from_user.id
    m = await c.bot.send_message(chat_id, "👤 Відкриваю профіль…")
    await send_profile(m, c.from_user.id, edit=True)


@router.callback_query(F.data.startswith("prof:likes_open:"))
async def prof_likes_open(c: CallbackQuery):
    try:
        idx = int(c.data.split(":")[-1])
    except Exception:
        idx = 0
    await open_likes_viewer(c, idx)


@router.callback_query(F.data.startswith("prof:likes_watch:"))
async def prof_likes_watch(c: CallbackQuery):
    if not c.message:
        await c.answer()
        return
    try:
        idx = int(c.data.split(":")[-1])
    except Exception:
        idx = 0

    total = get_liked_count(c.from_user.id)
    if total <= 0:
        await c.answer("Список обраних пуст 💔", show_alert=True)
        return

    idx = max(0, min(idx, total - 1))
    snap = get_liked_snapshot(c.from_user.id, idx)
    if not snap or not snap.get("watch_links"):
        await c.answer("Українських ресурсів не знайдено.", show_alert=True)
        return

    await c.answer()
    await safe_edit_reply_markup(c.message,
        reply_markup=kb_likes_watch(idx, snap["watch_links"])
    )


@router.callback_query(F.data.startswith("prof:likes_torrents:"))
async def prof_likes_torrents(c: CallbackQuery):
    """Показує торрент-посилання (Толока) в обраних"""
    if not c.message:
        await c.answer()
        return
    try:
        idx = int(c.data.split(":")[-1])
    except Exception:
        idx = 0

    total = get_liked_count(c.from_user.id)
    if total <= 0:
        await c.answer("Список обраних пуст 💔", show_alert=True)
        return

    idx = max(0, min(idx, total - 1))
    snap = get_liked_snapshot(c.from_user.id, idx)
    if not snap or not snap.get("watch_links"):
        await c.answer("Торрент-посилань не знайдено.", show_alert=True)
        return

    # Фільтруємо тільки Toloka
    torrent_links = [
        link for link in snap["watch_links"]
        if str(link.get("text") or "").lower() == "toloka"
    ]
    
    if not torrent_links:
        await c.answer("Торрент-посилань не знайдено.", show_alert=True)
        return

    await c.answer()
    await safe_edit_reply_markup(c.message,
        reply_markup=kb_likes_torrents(idx, snap["watch_links"])
    )

def get_total_translated_titles() -> int | None:
    conn = db()
    row = conn.execute(
        "SELECT value FROM bot_meta WHERE key=%s",
        ("total_translated_titles",),
    ).fetchone()
    if not row:
        return None
    try:
        return int(row[0])
    except Exception:
        return None

@router.message(Command("profile"))
async def profile_cmd(m: Message):
    await send_profile(m, m.from_user.id, edit=False)

@router.callback_query(F.data == "prof:clear_menu")
async def prof_clear_menu(c: CallbackQuery):
    await c.answer()
    if c.message:
        await safe_edit_reply_markup(c.message, reply_markup=kb_profile_clear_menu())

@router.callback_query(F.data == "prof:back_to_profile")
async def prof_back_to_profile(c: CallbackQuery):
    await c.answer()
    if c.message:
        await safe_edit_reply_markup(c.message, reply_markup=kb_profile())

@router.callback_query(F.data == "prof:clear_likes")
async def prof_clear_likes(c: CallbackQuery):
    count = clear_likes(c.from_user.id)
    if count > 0:
        await c.answer("Список обраних очищено✅", show_alert=True)
        if c.message:
            await send_profile(c.message, c.from_user.id, edit=True)
    else:
        await c.answer("Список обраних вже порожній 🤷‍♂️", show_alert=True)

def clear_user_history(user_id: int) -> int:
    # Транзакція гарантує: або ВСЕ видалиться, або НІЧОГО (якщо помилка)
    with transaction():
        conn = db()
        cur = conn.execute("DELETE FROM user_seen WHERE user_id=%s", (user_id,))
        conn.execute("UPDATE user_state SET last_page=NULL, updated_at=%s WHERE user_id=%s", (int(time.time()), user_id))
        return cur.rowcount

@router.callback_query(F.data == "prof:clear_history")
async def prof_clear_history(c: CallbackQuery):
    count = clear_user_history(c.from_user.id)
    if count > 0:
        await c.answer("Історію очищено ✅", show_alert=True)
        if c.message:
            await send_profile(c.message, c.from_user.id, edit=True)
    else:
        await c.answer("Історія рекомендацій вже порожня 🤷‍♂️", show_alert=True)

@router.callback_query(F.data == "prof:noop")
async def prof_noop(c: CallbackQuery):
    await c.answer()


# =========================
# Hikka OAuth handlers
# =========================

@router.callback_query(HikkaCB.filter(F.action == "status"))
async def cb_hikka_status(c: CallbackQuery):
    """Показує статус підключення до Hikka"""
    user_id = c.from_user.id
    token_data = get_hikka_token(user_id)

    if token_data:
        token, username = token_data
        name_display = f"<b>{username}</b>" if username else "підключено"
        text = (
            f"<tg-emoji emoji-id=\"5292247247453457908\">🔗</tg-emoji> <b>Hikka</b>\n\n"
            f"Статус: ✅ {name_display}\n\n"
            f"При натисканні ❤️ аніме автоматично додається "
            f"у <b>Заплановані</b> на Hikka."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚪 Вийти з Hikka", callback_data=HikkaCB(action="logout").pack())],
            [InlineKeyboardButton(text="« Назад", callback_data=SettingsCB(action="menu").pack())],
        ])
    else:
        text = (
            f"<tg-emoji emoji-id=\"5292247247453457908\">🔗</tg-emoji> <b>Hikka</b>\n\n"
            f"Статус: ❌ не підключено\n\n"
            f"Підключи свій акаунт Hikka, щоб при натисканні ❤️ "
            f"аніме автоматично додавалось у список "
            f"<b>Заплановані</b> на hikka.io."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Увійти в Hikka", callback_data=HikkaCB(action="login").pack())],
            [InlineKeyboardButton(text="« Назад", callback_data=SettingsCB(action="menu").pack())],
        ])

    await c.answer()
    await safe_edit_text(c.message, text, reply_markup=kb)


@router.callback_query(HikkaCB.filter(F.action == "login"))
async def cb_hikka_login(c: CallbackQuery, hikka_auth: HikkaAuth):
    """Надсилає юзеру URL для авторизації на Hikka"""
    if not hikka_auth.is_configured:
        await c.answer("Налаштування Hikka OAuth не знайдено.", show_alert=True)
        return

    auth_url = hikka_auth.get_auth_url()
    text = (
        f"🔑 <b>Вхід в Hikka</b>\n\n"
        f"1️⃣ Натисни кнопку нижче і увійди в свій акаунт на Hikka\n"
        f"2️⃣ Підтверди доступ для бота\n"
        f"3️⃣ Тебе перенаправить назад в бота автоматично\n\n"
        f"<i>Після авторизації аніме з ❤️ буде синхронізуватись "
        f'зі списком "Заплановані" на Hikka.</i>'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Відкрити Hikka", url=auth_url)],
        [InlineKeyboardButton(text="« Назад", callback_data=HikkaCB(action="status").pack())],
    ])

    await c.answer()
    await safe_edit_text(c.message, text, reply_markup=kb)

    # Зберігаємо message_id щоб потім відредагувати це повідомлення
    # на результат авторизації (замість надсилання нового)
    from api.hikka_auth import save_hikka_login_msg
    save_hikka_login_msg(c.from_user.id, c.message.message_id)


@router.callback_query(HikkaCB.filter(F.action == "logout"))
async def cb_hikka_logout(c: CallbackQuery):
    """Видаляє Hikka токен (logout)"""
    user_id = c.from_user.id
    delete_hikka_token(user_id)
    await c.answer("Вийшли з Hikka ✅", show_alert=True)

    # Показуємо оновлений статус
    text = (
        f"<tg-emoji emoji-id=\"5292247247453457908\">🔗</tg-emoji> <b>Hikka</b>\n\n"
        f"Статус: ❌ не підключено\n\n"
        f"Підключи свій акаунт Hikka, щоб при натисканні ❤️ "
        f"аніме автоматично додавалось у список "
        f"<b>Заплановані</b> на hikka.io."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Увійти в Hikka", callback_data=HikkaCB(action="login").pack())],
        [InlineKeyboardButton(text="« Назад", callback_data=SettingsCB(action="menu").pack())],
    ])
    await safe_edit_text(c.message, text, reply_markup=kb)

