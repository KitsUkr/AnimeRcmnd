import json
import uuid
import time
import aiohttp
from typing import List, Dict
from utils.callbacks import GenreCB, MenuCB

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest

from database.connection import db, transaction
from utils.ui_shared import Anime, format_caption, kb_for_anime
from utils.safe_edit import safe_edit_text, safe_edit_media, safe_edit_reply_markup

GENRE_MAP: dict[str, str] = {}
router = Router()

def save_genre_mapping(genres_data: list) -> List[str]:
    global GENRE_MAP
    GENRE_MAP.clear()
    
    names = []
    for g in genres_data:
        if isinstance(g, dict):
            slug = g.get("slug", "")
            name_ua = g.get("name_ua") or g.get("name_en") or slug
            if slug and name_ua:
                GENRE_MAP[slug] = str(name_ua).strip()
                names.append(str(name_ua).strip())
    
    # print(f"[GENRES] Збережено маппінг: {len(GENRE_MAP)} жанрів")
    return names

def sync_genre_mapping(genres_data: list = None) -> None:
    """
    Синхронізує GENRE_MAP зі свіжими даними з bot_meta.
    Викликається після синхронізації жанрів у sync_library().
    
    Args:
        genres_data: Список жанрів для синхронізації (опціонально).
                    Якщо не передано, завантажує з bot_meta.
    """
    global GENRE_MAP
    
    if genres_data is None:
        # Завантажуємо з bot_meta
        from api.hikka_client import meta_get, META_AVAILABLE_GENRES
        cached = meta_get(META_AVAILABLE_GENRES)
        if not cached:
            print("[GENRES] ⚠️ Дані жанрів не знайдені в bot_meta")
            return
        
        try:
            genres_data = json.loads(cached[0])
        except Exception as e:
            print(f"[GENRES] ❌ Помилка парсингу жанрів з bot_meta: {e}")
            return
    
    save_genre_mapping(genres_data)
def get_selected_genres(user_id: int) -> List[str]:
    conn = db()
    row = conn.execute("SELECT selected_genres_json FROM user_state WHERE user_id = %s", (user_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception as e:
        print(f"Error loading selected genres: {e}")
        return []

def set_selected_genres(user_id: int, genres: List[str]) -> None:
    json_str = json.dumps(sorted(set(genres)), ensure_ascii=False)
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, selected_genres_json, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                selected_genres_json = EXCLUDED.selected_genres_json,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, json_str, now),
        )

def get_excluded_genres(user_id: int) -> List[str]:
    conn = db()
    row = conn.execute("SELECT excluded_genres_json FROM user_state WHERE user_id = %s", (user_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception as e:
        print(f"Error loading excluded genres: {e}")
        return []

def set_excluded_genres(user_id: int, genres: List[str]) -> None:
    json_str = json.dumps(sorted(set(genres)), ensure_ascii=False)
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, excluded_genres_json, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                excluded_genres_json = EXCLUDED.excluded_genres_json,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, json_str, now),
        )

def toggle_genre(user_id: int, genre: str) -> List[str]:
    current = get_selected_genres(user_id)
    if genre in current:
        current.remove(genre)
    else:
        current.append(genre)
        # Ensure it's not in excluded
        excluded = get_excluded_genres(user_id)
        if genre in excluded:
            excluded.remove(genre)
            set_excluded_genres(user_id, excluded)
            
    set_selected_genres(user_id, current)
    return current

def clear_selected_genres(user_id: int) -> None:
    set_selected_genres(user_id, [])

def clear_excluded_genres(user_id: int) -> None:
    set_excluded_genres(user_id, [])

def get_genre_snapshot(user_id: int) -> List[str]:
    conn = db()
    row = conn.execute("SELECT genre_snapshot_json FROM user_state WHERE user_id = %s", (user_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception as e:
        print(f"Error loading genre snapshot: {e}")
        return []

def set_genre_snapshot(user_id: int, genres: List[str]) -> None:
    json_str = json.dumps(sorted(set(genres)), ensure_ascii=False)
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO user_state (user_id, genre_snapshot_json, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                genre_snapshot_json = EXCLUDED.genre_snapshot_json,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, json_str, now),
        )


# ==================== UI ====================
async def get_all_genres(hikka_client) -> List[str]:
    """
    Завантажує список всіх доступних жанрів.
    
    Приоритет:
    1. Читає з bot_meta (відомі кешовані жанри)
    2. Якщо bot_meta пуста (перший запуск): fallback-запрос до API
    3. Синхронізує GENRE_MAP для меню
    """
    from api.hikka_client import meta_get, META_AVAILABLE_GENRES
    
    # Приоритет 1: Читаємо з bot_meta
    cached = meta_get(META_AVAILABLE_GENRES)
    if cached:
        try:
            genres_data = json.loads(cached[0])
            sync_genre_mapping(genres_data)
            
            # Повертаємо назви жанрів
            names = []
            for g in genres_data:
                if isinstance(g, dict):
                    name_ua = g.get("name_ua") or g.get("name_en") or g.get("slug")
                    if name_ua:
                        names.append(str(name_ua).strip())
            
            return names
        except Exception as e:
            print(f"[GENRES] ⚠️ Помилка парсингу з bot_meta: {e}, спробую API fallback...")
    
    # Приоритет 2: Fallback - запрос до API (перший запуск)
    print("[GENRES] 📡 Первичное завантаження жанрів з API...")
    
    async with aiohttp.ClientSession() as session:
        genres_data = await hikka_client._fetch_genres_from_api(session)
    
    if genres_data:
        # Зберігаємо в bot_meta для кешування
        from api.hikka_client import meta_set, META_GENRES_VERSION, GENRES_VERSION
        genres_json = json.dumps(genres_data, ensure_ascii=False)
        meta_set(META_AVAILABLE_GENRES, genres_json)
        meta_set(META_GENRES_VERSION, GENRES_VERSION)
        
        # Синхронізуємо GENRE_MAP
        sync_genre_mapping(genres_data)
        
        # Повертаємо назви жанрів
        names = []
        for g in genres_data:
            if isinstance(g, dict):
                name_ua = g.get("name_ua") or g.get("name_en") or g.get("slug")
                if name_ua:
                    names.append(str(name_ua).strip())
        
        print(f"[GENRES] ✅ Завантажено {len(names)} жанрів з API")
        return names
    
    # Приоритет 3: Помилка - повертаємо порожній список
    print("[GENRES] ❌ Не вдалося завантажити жанри з API")
    return []

def get_slug_by_name(name: str) -> str:
    for slug, genre_name in GENRE_MAP.items():
        if genre_name == name:
            return slug
    return name

def get_name_by_slug(slug: str) -> str:
    return GENRE_MAP.get(slug, slug)

def toggle_genre_slug(user_id: int, genre_slug: str) -> None:
    # Tri-state logic: Neutral -> Included -> Excluded -> Neutral
    included = get_selected_genres(user_id)
    excluded = get_excluded_genres(user_id)
    
    if genre_slug in included:
        # Included -> Excluded
        included.remove(genre_slug)
        if genre_slug not in excluded:
            excluded.append(genre_slug)
        
        set_selected_genres(user_id, included)
        set_excluded_genres(user_id, excluded)
        
    elif genre_slug in excluded:
        # Excluded -> Neutral
        excluded.remove(genre_slug)
        set_excluded_genres(user_id, excluded)
        
    else:
        # Neutral -> Included
        # Ensure it's not in excluded (sanity check)
        if genre_slug in excluded:
            excluded.remove(genre_slug)
            set_excluded_genres(user_id, excluded)
            
        included.append(genre_slug)
        set_selected_genres(user_id, included)
    
    # Reset alert flag so user sees updated filter info
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(user_id)


ITEMS_PER_PAGE = 12

def kb_genres(included: List[str], excluded: List[str], genres: List[str], page: int = 0, snapshot_slugs: List[str] = None) -> InlineKeyboardMarkup:
    """
    snapshot_slugs: список slug-ів жанрів, які мають бути вгорі (sticky sort)
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # 1. Determine Sort Basis (тільки з snapshot, без fallback)
    sort_basis_set = set(snapshot_slugs) if snapshot_slugs else set()

    # 2. Sort Logic
    group_high = []
    group_low = []
    
    for g in genres:
        slug = get_slug_by_name(g)
        if slug in sort_basis_set:
            group_high.append(g)
        else:
            group_low.append(g)
            
    group_high.sort()
    group_low.sort()
    
    sorted_genres = group_high + group_low

    
    # 3. Pagination
    total_items = len(sorted_genres)
    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if page < 0: page = 0
    if page >= total_pages: page = max(0, total_pages - 1)
    
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    current_page_items = sorted_genres[start:end]
    
    row = []
    for genre_name in current_page_items:
        slug = get_slug_by_name(genre_name)
        
        # Tri-state icon logic
        if slug in included:
            text = f"✅ {genre_name}"
        elif slug in excluded:
            text = f"🚫 {genre_name}"
        else:
            text = genre_name
        
        # We pass "unified" as mode just to fill the field, though it's ignored now
        callback_data = GenreCB(action="toggle", mode="unified", slug=slug, page=page).pack()
        row.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        
        if len(row) == 2:
            kb.inline_keyboard.append(row)
            row = []
    
    if row:
        kb.inline_keyboard.append(row)

    # Navigation buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="«", callback_data=GenreCB(action="page", mode="unified", page=page-1).pack()))
    else:
        nav_row.append(InlineKeyboardButton(text="«", callback_data="noop:left"))
    
    nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop:page"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="»", callback_data=GenreCB(action="page", mode="unified", page=page+1).pack()))
    else:
        nav_row.append(InlineKeyboardButton(text="»", callback_data="noop:right"))
        
    kb.inline_keyboard.append(nav_row)

    # Actions
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="🗑️ Очистити все", callback_data=GenreCB(action="clear", mode="unified", page=page).pack()),
        InlineKeyboardButton(text="⬇️ Рекомендувати", callback_data=GenreCB(action="recommend").pack()), 
    ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(text="« Назад", callback_data="start:filters"), 
    ])
    
    return kb

@router.callback_query(F.data == "start:genres")
async def cb_open_genres(c: CallbackQuery, hikka_client):
    from UaAnimeRcmd import enter_genre_menu, is_genre_hint_shown, set_genre_hint_shown
    
    user_id = c.from_user.id
    
    # Show hint alert on first visit
    if not is_genre_hint_shown(user_id):
        hint_text = (
            "💡 Керування жанрами:\n\n"
            "• 1 клік → ✅ шукати\n"
            "• 2 кліки → 🚫 виключити\n"
            "• 3 кліки → скинути\n\n"
            "Або напишіть назву текстом!"
        )
        await c.answer(hint_text, show_alert=True)
        set_genre_hint_shown(user_id)
    else:
        await c.answer()
    
    # Unified View Entry
    genres = await get_all_genres(hikka_client)
    if not genres:
        await c.answer("Не вдалося завантажити жанри 😔", show_alert=True)
        return
        
    inc = get_selected_genres(user_id)
    exc = get_excluded_genres(user_id)
    
    # Store snapshot in DB
    snapshot = list(set(inc) | set(exc))
    set_genre_snapshot(user_id, snapshot)

    text = (
        "⚙️ <b>Керування Жанрами</b>\n\n"
        "• Натисніть <b>раз</b> (✅), щоб шукати тільки цей жанр.\n"
        "• Натисніть <b>два</b> (🚫), щоб виключити його.\n"
        "• Натисніть <b>три</b>, щоб скинути вибір.\n\n"
        "💡 <i>Або напишіть назву жанру текстом!</i>"
    )
    
    # Using unified mode signature
    kb = kb_genres(included=inc, excluded=exc, genres=genres, page=0, snapshot_slugs=snapshot)

    
    if c.message.photo:
        await c.message.delete()
        sent = await c.message.answer(text, reply_markup=kb)
    else:
        await safe_edit_text(c.message, text, reply_markup=kb)
        sent = c.message
    
    # Mark that user is in genre menu
    enter_genre_menu(user_id, sent.message_id)

@router.callback_query(GenreCB.filter(F.action == "page"))
async def cb_genre_page_nav(c: CallbackQuery, callback_data: GenreCB, hikka_client):
    page = callback_data.page or 0
    # sid is ignored
        
    genres = await get_all_genres(hikka_client)
    
    inc = get_selected_genres(c.from_user.id)
    exc = get_excluded_genres(c.from_user.id)
    snapshot = get_genre_snapshot(c.from_user.id)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb_genres(included=inc, excluded=exc, genres=genres, page=page, snapshot_slugs=snapshot))

@router.callback_query(GenreCB.filter(F.action == "toggle"))
async def cb_toggle_genre(c: CallbackQuery, callback_data: GenreCB, hikka_client):
    slug = callback_data.slug
    page = callback_data.page or 0
    # sid is ignored
    
    if not slug:
        await c.answer("Помилка: відсутній жанр")
        return
    
    # Unified toggle
    toggle_genre_slug(c.from_user.id, slug)
    
    genres = await get_all_genres(hikka_client)
    
    inc = get_selected_genres(c.from_user.id)
    exc = get_excluded_genres(c.from_user.id)
    snapshot = get_genre_snapshot(c.from_user.id)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb_genres(included=inc, excluded=exc, genres=genres, page=page, snapshot_slugs=snapshot))

@router.callback_query(GenreCB.filter(F.action == "clear"))
async def cb_genre_clear(c: CallbackQuery, callback_data: GenreCB, hikka_client):
    page = callback_data.page or 0
    # sid is ignored
        
    # Clear ALL
    clear_selected_genres(c.from_user.id) 
    clear_excluded_genres(c.from_user.id)
    # Clear Snapshot because nothing is selected anymore
    set_genre_snapshot(c.from_user.id, [])
    
    # Reset alert flag so user sees updated filter info
    from UaAnimeRcmd import reset_filter_alert
    reset_filter_alert(c.from_user.id)
    
    genres = await get_all_genres(hikka_client)
    
    await safe_edit_reply_markup(c.message, reply_markup=kb_genres(included=[], excluded=[], genres=genres, page=page, snapshot_slugs=[]))
    
    await c.answer("Всі фільтри скинуто! ✨", show_alert=True)


@router.callback_query(GenreCB.filter(F.action == "back"))
async def cb_genre_back(c: CallbackQuery, start_text: str, kb_start_func):
    # Exit genre menu state
    from UaAnimeRcmd import exit_genre_menu
    exit_genre_menu(c.from_user.id)
    
    await c.answer()
    await safe_edit_text(c.message, start_text, reply_markup=kb_start_func())


@router.callback_query(GenreCB.filter(F.action == "recommend"))
async def cb_genre_recommend(c: CallbackQuery, hikka_client, db_funcs: dict):
    # Exit genre menu state
    from UaAnimeRcmd import exit_genre_menu
    exit_genre_menu(c.from_user.id)
    
    user_id = c.from_user.id
    selected_slugs = get_selected_genres(user_id)
    excluded_gen_slugs = get_excluded_genres(user_id)  # ✅ Завантажуємо один раз
    
    if not selected_slugs:
        await c.answer("Виберіть хоча б один жанр! 😅", show_alert=True)
        return

    # Included
    # names_inc are loaded earlier

    from utils.ui_shared import get_filter_alert_text
    from handlers.filters.content import get_selected_content_types, get_excluded_content_types, CONTENT_TYPE_MAP

    types_inc = get_selected_content_types(user_id)
    types_exc = get_excluded_content_types(user_id)

    alert_text = get_filter_alert_text(
        selected_genres=selected_slugs,
        excluded_genres=excluded_gen_slugs,
        selected_types=types_inc,
        excluded_types=types_exc,
        genre_names=GENRE_MAP,
        type_names=CONTENT_TYPE_MAP
    )
    
    if alert_text:
        await c.answer(alert_text, show_alert=True)
    else:
        await c.answer()

    get_excluded = db_funcs['get_excluded_ids']
    get_last = db_funcs['get_last_page']
    set_last = db_funcs['set_last_page']
    mark_seen = db_funcs['mark_seen']
    save_cb_map = db_funcs['save_cb_map']

    excluded_ids = get_excluded(user_id)
    last_page = get_last(user_id)
    
    # Convert slugs to names for library search
    selected_genre_names = [get_name_by_slug(s) for s in selected_slugs]
    # ✅ Використовуємо excluded_gen_slugs з початку функції

    try:
        anime, used_page = await hikka_client.random_anime(
            exclude_ids=excluded_ids,
            last_page=last_page,
            genres=selected_slugs,
            genre_names=selected_genre_names,
            excluded_genres=excluded_gen_slugs  # ✅ single source of truth
        )
    except Exception as e:
        from api.hikka_client import FilteredAnimeExhaustedError
        if isinstance(e, FilteredAnimeExhaustedError):
            from utils.ui_shared import build_filter_exhausted_message
            msg = build_filter_exhausted_message(e)
            try:
                if c.message.content_type == "photo":
                    await c.message.delete()
                    await c.message.answer(msg)
                else:
                    await c.message.edit_text(msg)
            except Exception:
                try:
                    await c.answer("Ви переглянули все аніме за цими фільтрами!", show_alert=True)
                except:
                    pass
            return
        await safe_edit_text(c.message, f"Не вдалося знайти аніме у жанрах: {', '.join(selected_slugs)}\n\nПомилка: {e}")
        return

    cb_id = uuid.uuid4().hex[:12]
    
    with transaction():
        set_last(user_id, used_page)
        mark_seen(user_id, anime.id)
        save_cb_map(cb_id, anime)

    caption = format_caption(anime)
    # Uses ui_shared.kb_for_anime which uses AnimeCB now!
    kb = kb_for_anime(cb_id, has_filter=True)

    # Choose poster: UA > Hikka > None
    final_poster_url = anime.ua_poster_url if anime.ua_poster_url else anime.poster_url
    has_ua_poster = bool(anime.ua_poster_url)

    if final_poster_url:
        try:
            await c.message.edit_media(InputMediaPhoto(media=final_poster_url, caption=caption), reply_markup=kb)
            return
        except Exception as e:
            print(f"[GENRES] ❌ Не вдалося відправити постер {final_poster_url}: {e}")
            
            # Якщо це помилка Telegram і це UA постер - видаляємо його з бази
            if has_ua_poster:
                from UaAnimeRcmd import is_telegram_poster_error, remove_invalid_ua_poster
                if is_telegram_poster_error(e):
                    remove_invalid_ua_poster(anime.slug)
            
            # Fallback на Hikka постер якщо це був битий UA постер
            if has_ua_poster and anime.poster_url:
                print(f"[FALLBACK] Пробую Hikka постер в жанрах")
                try:
                    await c.message.edit_media(InputMediaPhoto(media=anime.poster_url, caption=caption), reply_markup=kb)
                    return
                except Exception as e2:
                    print(f"[FALLBACK] Hikka постер теж не вдався: {e2}")

    await safe_edit_text(c.message, caption, reply_markup=kb)


# ==================== Text-based genre search ====================
def find_genre_by_text(query: str) -> List[str]:
    query_lower = query.lower().strip()
    if not query_lower:
        return []
    
    exact = []
    starts_with = []
    contains = []
    
    for slug, name in GENRE_MAP.items():
        name_lower = name.lower()
        
        if name_lower == query_lower:
            exact.append(slug)
        elif name_lower.startswith(query_lower):
            starts_with.append(slug)
        elif query_lower in name_lower:
            contains.append(slug)
    
    return exact + starts_with + contains


from aiogram.types import Message

@router.message(F.text)
async def handle_genre_text_search(message: Message, hikka_client):
    """Handle text messages when user is in genre menu - supports multiple genres"""
    from UaAnimeRcmd import is_in_genre_menu, get_genre_menu_message_id, reset_filter_alert
    import asyncio
    import re
    
    user_id = message.from_user.id
    
    # Check if user is in genre menu
    if not is_in_genre_menu(user_id):
        return  # Not in genre menu, ignore
    
    raw_text = message.text.strip()
    if not raw_text:
        return
    
    # Delete user's message
    try:
        await message.delete()
    except Exception:
        pass
    
    # Ensure genres are loaded
    await get_all_genres(hikka_client)
    
    # Split by comma, newline, or spaces
    # "романтика, комедія" or "романтика комедія" or "романтика\nкомедія"
    parts = re.split(r'[,\n]+', raw_text)
    queries = []
    for part in parts:
        # Split by spaces
        sub_parts = part.strip().split()
        for sp in sub_parts:
            sp = sp.strip()
            if sp:
                queries.append(sp)
    
    if not queries:
        return
    
    # Process each query
    added = []           # Successfully added
    already_selected = [] # Already in included
    not_found = []       # No match
    ambiguous = []       # Multiple matches
    
    inc = get_selected_genres(user_id)
    exc = get_excluded_genres(user_id)
    snapshot = get_genre_snapshot(user_id)
    
    for query in queries:
        matches = find_genre_by_text(query)
        
        if not matches:
            not_found.append(query)
            continue
        
        if len(matches) > 1:
            # Check if first match is exact
            first_name = GENRE_MAP.get(matches[0], matches[0]).lower()
            if first_name == query.lower():
                # Exact match, use it
                matched_slug = matches[0]
            else:
                ambiguous.append((query, [GENRE_MAP.get(s, s) for s in matches[:3]]))
                continue
        else:
            matched_slug = matches[0]
        
        matched_name = GENRE_MAP.get(matched_slug, matched_slug)
        
        if matched_slug in inc:
            already_selected.append(matched_name)
        else:
            # Add to included
            inc.append(matched_slug)
            added.append(matched_name)
            
            # Remove from excluded if was there
            if matched_slug in exc:
                exc.remove(matched_slug)
            
            # Add to snapshot for sticky sort
            if matched_slug not in snapshot:
                snapshot.append(matched_slug)
    
    # Save changes if any added
    if added:
        set_selected_genres(user_id, inc)
        set_excluded_genres(user_id, exc)
        set_genre_snapshot(user_id, snapshot)
        reset_filter_alert(user_id)
    
    # Build feedback message
    feedback_parts = []
    if added:
        feedback_parts.append("✅ Обрано: " + ", ".join(added))
    if already_selected:
        feedback_parts.append("ℹ️ Вже обрано: " + ", ".join(already_selected))
    if not_found:
        feedback_parts.append("❌ Не знайдено: " + ", ".join(not_found))
    if ambiguous:
        for q, opts in ambiguous:
            feedback_parts.append(f"🔍 \"{q}\" — уточніть: {', '.join(opts)}")
    
    # Update the genre menu message
    menu_message_id = get_genre_menu_message_id(user_id)
    if menu_message_id and added:
        genres = await get_all_genres(hikka_client)
        text = (
            "⚙️ <b>Керування Жанрами</b>\n\n"
            "• Натисніть <b>раз</b> (✅), щоб шукати тільки цей жанр.\n"
            "• Натисніть <b>два</b> (🚫), щоб виключити його.\n"
            "• Натисніть <b>три</b>, щоб скинути вибір.\n\n"
            "💡 <i>Або напишіть назву жанру текстом!</i>"
        )
        kb = kb_genres(included=inc, excluded=exc, genres=genres, page=0, snapshot_slugs=snapshot)
        
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                text=text,
                reply_markup=kb
            )
        except TelegramBadRequest:
            pass
    
    # Send feedback
    if feedback_parts:
        confirm = await message.answer("\n".join(feedback_parts))
        await asyncio.sleep(3)
        try:
            await confirm.delete()
        except Exception:
            pass
