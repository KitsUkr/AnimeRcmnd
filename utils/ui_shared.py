import html
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import texts as t

_TAG_RE = re.compile(r"<[^>]+>")

# Pattern to match allowed HTML tags that should be preserved
_ALLOWED_TAGS = re.compile(r"(</?(?:b|i|u|s|code|pre)>)", re.IGNORECASE)

def escape_preserve_html(text: str) -> str:
    """Escape HTML special characters but preserve allowed formatting tags (<b>, <i>, etc.)"""
    if not text:
        return ""
    
    # Split by allowed tags, escape parts between them, then rejoin
    parts = _ALLOWED_TAGS.split(text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # This is text between tags - escape it
            result.append(html.escape(part))
        else:
            # This is a tag - keep it as is
            result.append(part)
    return "".join(result)


def safe_loads_list(json_str: Any, context: str = "data") -> List:
    """Safely parse JSON string to list with error handling.
    
    Args:
        json_str: JSON string or None
        context: Description for error logging
        
    Returns:
        Parsed list or empty list on error
    """
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, TypeError) as e:
        print(f"Error loading {context}: {e}")
        return []

@dataclass
class Anime:
    id: str
    slug: str
    title: str
    year: Optional[int]
    score: Optional[float]
    genres: List[str]
    episodes_total: Optional[int]
    description: Optional[str]
    poster_url: Optional[str]
    hikka_url: Optional[str]
    watch_links: List[Dict[str, str]]
    ua_poster_url: Optional[str] = None
    content_type: Optional[str] = None  # tv, movie, special, ova, etc.
    season: Optional[str] = None

MAX_CAPTION = 1024

def visible_len(s: str) -> int:
    if not s:
        return 0
    no_tags = _TAG_RE.sub("", s)
    return len(html.unescape(no_tags))

def cut_plain_to_limit(plain: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(plain) <= limit:
        return plain
    if limit == 1:
        return "…"
    return plain[: limit - 1] + "…"

def cut_html_to_visible_limit(html_text: str, limit: int) -> str:
    """Cut HTML text to visible character limit, preserving valid HTML tags."""
    if limit <= 0:
        return ""
    if visible_len(html_text) <= limit:
        return html_text
    if limit == 1:
        return "…"
    
    result = []
    visible_count = 0
    i = 0
    open_tags = []  # Stack to track open tags
    
    while i < len(html_text) and visible_count < limit - 1:
        if html_text[i] == '<':
            # Find the end of the tag
            tag_end = html_text.find('>', i)
            if tag_end == -1:
                break
            tag = html_text[i:tag_end + 1]
            result.append(tag)
            
            # Track open/close tags
            if tag.startswith('</'):
                # Closing tag
                tag_name = tag[2:-1].lower().strip()
                if open_tags and open_tags[-1] == tag_name:
                    open_tags.pop()
            elif not tag.endswith('/>'):
                # Opening tag (not self-closing)
                # Extract tag name
                tag_content = tag[1:-1].strip()
                tag_name = tag_content.split()[0].lower() if tag_content else ""
                if tag_name in ('b', 'i', 'u', 's', 'code', 'pre'):
                    open_tags.append(tag_name)
            
            i = tag_end + 1
        else:
            result.append(html_text[i])
            visible_count += 1
            i += 1
    
    result.append("…")
    
    # Close any remaining open tags in reverse order
    for tag_name in reversed(open_tags):
        result.append(f"</{tag_name}>")
    
    return "".join(result)

def format_caption(a: Anime, max_length: int = MAX_CAPTION) -> str:
    from handlers.filters.content import get_name_by_slug
    year = f" ({a.year})" if a.year else ""
    score = f"⭐ <b>{a.score:.1f}</b>" if isinstance(a.score, float) else ""
    eps = f" • <b>{a.episodes_total}</b> {t.CAPTION_EPISODES}" if a.episodes_total else ""
    ctype = f" • {get_name_by_slug(a.content_type)}" if a.content_type else ""
    genres = f"\n\n{t.CAPTION_GENRES_LABEL} <i>{', '.join(a.genres)}</i>" if a.genres else ""
    link_line = f"\n<a href=\"{a.hikka_url}\">{t.CAPTION_HIKKA_LINK}</a>" if a.hikka_url else ""

    base = (
        f"<b>{a.title}</b>{year}\n"
        f"{score}{eps}{ctype}{genres}"
    )

    if not isinstance(a.description, str) or not a.description.strip():
        return base + link_line

    # Description already contains HTML tags (<b>, <i>) from clean_synopsis
    synopsis_html = a.description.strip().replace("\n", " ")
    # Calculate visible length (without HTML tags)
    synopsis_visible_len = visible_len(synopsis_html)

    synopsis_prefix_short = f"\n\n<i>{t.CAPTION_SYNOPSIS_LABEL}</i> "
    synopsis_prefix_long = f"\n\n<i>{t.CAPTION_SYNOPSIS_LABEL}</i>\n<blockquote expandable>"
    synopsis_suffix_long = "</blockquote>"

    used = visible_len(base) + visible_len(link_line)

    if synopsis_visible_len >= 281:
        available = max_length - used - visible_len(synopsis_prefix_long) - visible_len(synopsis_suffix_long)
        if available > 50:
            # Cut the synopsis to available length, preserving HTML tags
            cut_synopsis = cut_html_to_visible_limit(synopsis_html, available)
            return base + synopsis_prefix_long + cut_synopsis + synopsis_suffix_long + link_line

    available = max_length - used - visible_len(synopsis_prefix_short)
    if available > 20:
        cut_synopsis = cut_html_to_visible_limit(synopsis_html, available)
        return base + synopsis_prefix_short + cut_synopsis + link_line

    return base + link_line

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils.callbacks import AnimeCB, MenuCB

def kb_for_anime(cb_id: str, has_filter: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=t.BTN_MORE, callback_data=MenuCB(action="random").pack()),
            InlineKeyboardButton(text=t.BTN_WHERE_TO_WATCH, callback_data=AnimeCB(action="watch", id=cb_id).pack())
        ],
        [
            InlineKeyboardButton(text=t.BTN_ADD_FAVORITE, callback_data=AnimeCB(action="like", id=cb_id).pack())
        ]
    ]
    
    if has_filter:
        rows.append([InlineKeyboardButton(text=t.BTN_CHANGE_FILTER, callback_data="start:filters")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_filter_alert_text(
    selected_genres: List[str] = [], 
    excluded_genres: List[str] = [],
    selected_types: List[str] = [],
    excluded_types: List[str] = [],
    genre_names: Dict[str, str] = {},
    type_names: Dict[str, str] = {},
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    seasons: List[str] = [],
    season_names: Dict[str, str] = {},
    rating_min: Optional[float] = None,
) -> Optional[str]:
    parts = []
    
    if selected_genres:
        names = [genre_names.get(s, s) for s in selected_genres]
        genre_list = "– " + ", ".join(names)
        parts.append(t.FILTER_ALERT_GENRES.format(genre_list=genre_list))
        
    if selected_types:
        names = [type_names.get(s, s) for s in selected_types]
        type_list = "– " + ", ".join(names)
        parts.append(t.FILTER_ALERT_TYPES.format(type_list=type_list))
        
    if excluded_genres:
        names = [genre_names.get(s, s) for s in excluded_genres]
        genre_list = "– " + ", ".join(names)
        parts.append(t.FILTER_ALERT_EXCLUDED_GENRES.format(genre_list=genre_list))
        
    if excluded_types:
        names = [type_names.get(s, s) for s in excluded_types]
        type_list = "– " + ", ".join(names)
        parts.append(t.FILTER_ALERT_EXCLUDED_TYPES.format(type_list=type_list))
    
    if year_from or year_to:
        if year_from and year_to:
            parts.append(t.FILTER_ALERT_YEARS_RANGE.format(year_from=year_from, year_to=year_to))
        elif year_from:
            parts.append(t.FILTER_ALERT_YEARS_FROM.format(year_from=year_from))
        else:
            parts.append(t.FILTER_ALERT_YEARS_TO.format(year_to=year_to))
            
    if seasons:
        names = [season_names.get(s, s) for s in seasons]
        season_list = "– " + ", ".join(names)
        parts.append(t.FILTER_ALERT_SEASONS.format(season_list=season_list))

    if rating_min is not None:
        parts.append(t.FILTER_ALERT_RATING_MIN.format(rating_min=rating_min))
        
    if not parts:
        return None
        
    return "\n\n".join(parts)


def build_filter_exhausted_message(e) -> str:
    """
    Формує повідомлення, коли юзер переглянув все аніме за обраними фільтрами.
    e: FilteredAnimeExhaustedError з полями genre_names, content_types, year_from, year_to
    """
    from handlers.filters.content import CONTENT_TYPE_MAP

    parts = []

    if e.genre_names:
        genre_str = ", ".join(e.genre_names)
        if len(e.genre_names) == 1:
            parts.append(t.FILTER_EXHAUSTED_IN_GENRE.format(genres=genre_str))
        else:
            parts.append(t.FILTER_EXHAUSTED_IN_GENRES.format(genres=genre_str))

    if e.content_types:
        type_names = [CONTENT_TYPE_MAP.get(ct, ct) for ct in e.content_types]
        type_str = ", ".join(type_names)
        parts.append(t.FILTER_EXHAUSTED_WITH_TYPE.format(types=type_str))

    if e.year_from or e.year_to:
        if e.year_from and e.year_to:
            parts.append(t.FILTER_EXHAUSTED_YEARS_RANGE.format(year_from=e.year_from, year_to=e.year_to))
        elif e.year_from:
            parts.append(t.FILTER_EXHAUSTED_YEARS_FROM.format(year_from=e.year_from))
        else:
            parts.append(t.FILTER_EXHAUSTED_YEARS_TO.format(year_to=e.year_to))

    if hasattr(e, 'seasons') and e.seasons:
        # Avoid circular imports but get names if possible
        # We can just check SEASON_MAP directly or fallback to slug
        from handlers.filters.season import SEASON_MAP
        season_names = [SEASON_MAP.get(s, s) for s in e.seasons]
        season_str = ", ".join(season_names)
        parts.append(t.FILTER_EXHAUSTED_SEASONS.format(seasons=season_str))

    if hasattr(e, 'score_min') and e.score_min is not None:
        parts.append(t.FILTER_EXHAUSTED_SCORE.format(score_min=e.score_min))

    if parts:
        filter_desc = " ".join(parts)
        msg = t.FILTER_EXHAUSTED_WITH_DESC.format(filter_desc=filter_desc)
    else:
        msg = t.FILTER_EXHAUSTED_GENERIC

    return msg