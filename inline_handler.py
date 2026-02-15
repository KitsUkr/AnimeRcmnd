"""
Inline Mode Handler - дозволяє шукати аніме з будь-якого чату.
Використання: @bot_username <назва аніме>
"""
import json
import hashlib
from typing import List, Optional, Dict

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from database import db
from ui_shared import Anime, format_caption, MAX_CAPTION

router = Router()

# Максимальна кількість результатів (Telegram ліміт: 50)
MAX_RESULTS = 20

# Ліміт для caption фото (Telegram: 1024)
PHOTO_CAPTION_LIMIT = 1024


def search_anime_by_title(query: str, limit: int = MAX_RESULTS) -> List[tuple]:
    """
    Пошук аніме по назві в локальній БД.
    Повертає список tuple з даними аніме та watch_links.
    """
    conn = db()
    
    if not query.strip():
        # Порожній запит — повертаємо випадкові аніме
        rows = conn.execute(
            """
            SELECT l.slug, l.title, l.genres_json, l.score, l.year, l.episodes_total, 
                   l.poster_url, l.hikka_url, l.ua_poster_url, l.content_type,
                   COALESCE(d.description, '') as description,
                   COALESCE(d.watch_links_json, '[]') as watch_links_json
            FROM anime_library l
            LEFT JOIN anime_details_cache d ON l.slug = d.slug
            WHERE l.score IS NOT NULL AND l.score > 0
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (limit,)
        ).fetchall()
    else:
        # Пошук по назві (ILIKE для case-insensitive)
        search_pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT l.slug, l.title, l.genres_json, l.score, l.year, l.episodes_total, 
                   l.poster_url, l.hikka_url, l.ua_poster_url, l.content_type,
                   COALESCE(d.description, '') as description,
                   COALESCE(d.watch_links_json, '[]') as watch_links_json
            FROM anime_library l
            LEFT JOIN anime_details_cache d ON l.slug = d.slug
            WHERE l.title ILIKE %s
              AND l.score IS NOT NULL AND l.score > 0
            ORDER BY l.score DESC
            LIMIT %s
            """,
            (search_pattern, limit)
        ).fetchall()
    
    results = []
    for row in rows:
        slug, title, genres_json, score, year, episodes, poster, hikka_url, ua_poster, content_type, description, watch_links_json = row
        
        # Парсимо жанри
        try:
            genres = json.loads(genres_json) if genres_json else []
        except:
            genres = []
        
        # Парсимо watch_links
        try:
            watch_links = json.loads(watch_links_json) if watch_links_json else []
        except:
            watch_links = []
        
        # Вибираємо UA постер якщо є
        final_poster = ua_poster or poster
        
        anime = Anime(
            id=slug,
            slug=slug,
            title=title or "Без назви",
            genres=genres[:8],
            score=float(score) if score else None,
            year=int(year) if year else None,
            episodes_total=int(episodes) if episodes else None,
            poster_url=final_poster,
            hikka_url=hikka_url,
            description=description if description else None,
            watch_links=watch_links,
            ua_poster_url=ua_poster,
            content_type=content_type,
        )
        
        results.append(anime)
    
    return results


def format_short_description(anime: Anime) -> str:
    """Форматує короткий опис для превью inline-результату."""
    parts = []
    
    if anime.score:
        parts.append(f"⭐ {anime.score:.1f}")
    
    if anime.year:
        parts.append(f"📅 {anime.year}")
    
    if anime.episodes_total:
        parts.append(f"📺 {anime.episodes_total} еп.")
    
    if anime.content_type:
        type_map = {
            "tv": "TV",
            "movie": "Фільм",
            "ova": "OVA",
            "ona": "ONA",
            "special": "Спешл",
            "music": "Музика",
        }
        parts.append(type_map.get(anime.content_type, anime.content_type))
    
    return " • ".join(parts) if parts else "Аніме"


def is_valid_url(url: Optional[str]) -> bool:
    """Перевіряє чи URL валідний для використання."""
    return bool(url and url.strip().startswith("http"))


def build_watch_keyboard(anime: Anime) -> Optional[InlineKeyboardMarkup]:
    """Створює клавіатуру з кнопками 'Де дивитись'."""
    buttons = []
    
    # Додаємо кнопки для кожного сайту перегляду (без Toloka)
    for link in anime.watch_links:
        name = link.get("text") or link.get("name") or "Дивитись"
        url = link.get("url", "")
        
        # Пропускаємо Toloka
        if name.lower() == "toloka":
            continue
            
        if url and is_valid_url(url) and len(buttons) < 4:  # Максимум 4 кнопки
            buttons.append([InlineKeyboardButton(text=f"» {name} «", url=url)])
    
    # Якщо немає watch_links, додаємо посилання на Hikka
    if not buttons and anime.hikka_url:
        buttons.append([InlineKeyboardButton(text="🔎 Сторінка на Hikka", url=anime.hikka_url)])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None



@router.inline_query()
async def inline_search(query: InlineQuery):
    """
    Обробник inline-запитів.
    Користувач вводить: @bot_username <пошуковий запит>
    """
    search_text = query.query.strip()
    
    # Пошук аніме
    if search_text:
        anime_list = search_anime_by_title(search_text, limit=MAX_RESULTS)
    else:
        # Порожній запит — показуємо 1 випадкове аніме (справжній рандом!)
        anime_list = search_anime_by_title("", limit=1)
    
    results = []
    
    if not anime_list:
        # Нічого не знайдено — показуємо повідомлення
        results.append(
            InlineQueryResultArticle(
                id="not_found",
                title="❌ Нічого не знайдено",
                description=f"Спробуйте інший запит: \"{search_text[:30]}...\"" if len(search_text) > 30 else f"Запит: \"{search_text}\"",
                input_message_content=InputTextMessageContent(
                    message_text=f"🔍 Пошук \"{search_text}\" не дав результатів.",
                    parse_mode="HTML"
                ),
            )
        )
    else:
        for anime in anime_list:
            # Унікальний ID для результату (MD5 від slug)
            result_id = hashlib.md5(anime.slug.encode()).hexdigest()[:16]
            
            poster_url = anime.poster_url
            
            # Використовуємо той самий format_caption що й в боті!
            caption = format_caption(anime, max_length=PHOTO_CAPTION_LIMIT)
            
            # Кнопки "Де дивитись"
            reply_markup = build_watch_keyboard(anime)
            
            if is_valid_url(poster_url):
                # Завжди використовуємо Photo для повноцінного постера
                title = f"🎲 {anime.title}" if not search_text else anime.title
                results.append(
                    InlineQueryResultPhoto(
                        id=result_id,
                        photo_url=poster_url,
                        thumbnail_url=poster_url,
                        title=title,
                        description=format_short_description(anime),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                )
            else:
                # Без постера — текстовий результат
                results.append(
                    InlineQueryResultArticle(
                        id=result_id,
                        title=anime.title,
                        description=format_short_description(anime),
                        input_message_content=InputTextMessageContent(
                            message_text=caption,
                            parse_mode="HTML"
                        ),
                        reply_markup=reply_markup,
                    )
                )
    
    # Відповідаємо на inline-запит
    # cache_time=0 — без кешування для debug (потім можна повернути на 30)
    await query.answer(
        results=results,
        cache_time=0,
        is_personal=True  # Персональні результати для debug
    )
