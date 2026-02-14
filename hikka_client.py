import aiohttp
import asyncio
import json
import random
import time
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, quote
from database import db, transaction
from sql_queries import INSERT_LIBRARY_ITEM, INSERT_LIBRARY_ITEM_SKIP_POSTER_UPDATE, COUNT_LIBRARY, UPDATE_LIBRARY_GENRES
from ui_shared import Anime
from genre_filters import get_name_by_slug, get_slug_by_name


class AllAnimeSeenError(Exception):
    """Raised when user has seen all anime in the library (no filters)."""
    def __init__(self, total_count: int):
        self.total_count = total_count
        super().__init__(f"All {total_count} anime have been seen")

# Helper Constants
META_TOTAL_TRANSLATED = "total_translated_titles"
META_LAST_LIBRARY_SYNC = "last_library_sync"
META_LAST_POSTERS_SYNC = "last_posters_sync"
META_AVAILABLE_GENRES = "available_genres_json"

DETAILS_TTL_SECONDS = 12 * 24 * 3600  # 12 days
LIBRARY_SYNC_INTERVAL = 13 * 24 * 3600 # 13 days
POSTER_SYNC_INTERVAL = 14 * 24 * 3600 # 14 days

# Regex patterns
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SOURCE_LINE = re.compile(r"\n?\s*джерело.*$", re.IGNORECASE | re.DOTALL)
# Markdown formatting patterns (convert to HTML for Telegram)
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")  # **text** -> <b>text</b>
_MD_ITALIC = re.compile(r"__([^_]+)__")  # __text__ -> <i>text</i>
_MD_GUILLEMETS_BOLD = re.compile(r"\*\*«([^»]+)»\*\*")  # **«text»** -> <b>«text»</b>

# Helpers
def _to_int(v: Any) -> Optional[int]:
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        s = str(v).strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None

def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(",", ".")
        return float(s)
    except Exception:
        return None

def clean_synopsis(text: str) -> str:
    if not isinstance(text, str):
        return ""

    # Remove MD links but keep link text
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _SOURCE_LINE.sub("", text)
    
    # Convert Markdown formatting to HTML (for Telegram)
    # Process bold first (**text**), then italic (*text*)
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC.sub(r"<i>\1</i>", text)
    
    text = text.replace("\n", " ")
    text = re.sub(r"\s{2,}", " ", text).strip()

    return text


async def url_exists(session: aiohttp.ClientSession, url: str) -> bool:
    if not url:
        return False

    try:
        async with session.head(url, allow_redirects=True, timeout=15) as r:
            if r.status == 200:
                return True
            if r.status == 404:
                return False
    except Exception:
        pass

    try:
        # fallback: short GET to catch 404
        async with session.get(
            url,
            allow_redirects=True,
            timeout=20,
            headers={"Range": "bytes=0-1"},
        ) as r:
            if r.status == 404:
                return False
            return 200 <= r.status < 400
    except Exception:
        return False

def meta_get(key: str) -> tuple[str, int] | None:
    conn = db()
    row = conn.execute(
        "SELECT value, updated_at FROM bot_meta WHERE key=%s",
        (key,),
    ).fetchone()
    if not row:
        return None
    return str(row[0]), int(row[1])

def meta_set(key: str, value: str) -> None:
    now = int(time.time())
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO bot_meta(key, value, updated_at)
            VALUES(%s, %s, %s)
            ON CONFLICT(key) DO UPDATE SET
                value=EXCLUDED.value,
                updated_at=EXCLUDED.updated_at
            """,
            (key, value, now),
        )

def get_cached_details(slug: str) -> Optional[Tuple[Optional[str], List[Dict[str, str]]]]:
    """Отримати закешовані деталі (опис, лінки). Жанри тепер в anime_library."""
    now = int(time.time())
    conn = db()
    row = conn.execute(
        """
        SELECT description, watch_links_json, expires_at
        FROM anime_details_cache
        WHERE slug=%s
        """,
        (slug,),
    ).fetchone()

    if not row:
        return None

    description, watch_links_json, expires_at = row
    if expires_at is not None and int(expires_at) < now:
        return None

    try:
        wl = json.loads(watch_links_json) if watch_links_json else []
        if not isinstance(wl, list):
            wl = []
    except Exception as e:
        print(f"Error loading cached watch links: {e}")
        wl = []

    # Apply clean_synopsis to convert markdown to HTML (for cached data before this fix)
    clean_desc = clean_synopsis(str(description)) if description else None
    return clean_desc, wl

def set_cached_details(slug: str, description: Optional[str], watch_links: List[Dict[str, str]]) -> None:
    """Зберегти деталі в кеш (опис, лінки). Жанри тепер в anime_library."""
    now = int(time.time())
    expires_at = now + DETAILS_TTL_SECONDS
    with transaction():
        conn = db()
        conn.execute(
            """
            INSERT INTO anime_details_cache(
                slug, description, watch_links_json, updated_at, expires_at
            )
            VALUES(%s, %s, %s, %s, %s)
            ON CONFLICT(slug) DO UPDATE SET
                description=EXCLUDED.description,
                watch_links_json=EXCLUDED.watch_links_json,
                updated_at=EXCLUDED.updated_at,
                expires_at=EXCLUDED.expires_at
            """,
            (
                slug, 
                description, 
                json.dumps(watch_links or [], ensure_ascii=False),
                now, 
                expires_at
            ),
        )

async def get_or_refresh_watch_links(slug: str, hikka_client: "HikkaClient") -> List[Dict[str, str]]:
    """Отримати watch_links з кешу або оновити з API якщо TTL вичерпано."""
    import aiohttp
    
    # 1. Спробувати з кешу (TTL перевіряється всередині)
    cached = get_cached_details(slug)
    if cached:
        _, watch_links = cached
        if watch_links:
            return watch_links
    
    # 2. TTL вичерпано або немає даних — йдемо на API
    try:
        async with aiohttp.ClientSession() as session:
            await hikka_client.load_details(session, slug)
            watch_links = hikka_client.watch_links or []
            description = hikka_client.description
            
            # Оновлюємо кеш з новим TTL
            set_cached_details(slug, description, watch_links)
            
            return watch_links
    except Exception as e:
        print(f"[WATCH_LINKS] Помилка оновлення для {slug}: {e}")
        
        # 3. Fallback: повернути старі дані з кешу навіть якщо TTL вичерпано
        conn = db()
        row = conn.execute(
            "SELECT watch_links_json FROM anime_details_cache WHERE slug=%s",
            (slug,),
        ).fetchone()
        if row and row[0]:
            try:
                wl = json.loads(row[0])
                if isinstance(wl, list):
                    return wl
            except Exception:
                pass
        
        return []


class HikkaClient:
    PREFIXES = ("", "/api", "/v1", "/api/v1")
    PATH = "/anime"

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._resolved_url: Optional[str] = None
        self._total_pages: Optional[int] = None
        self.ua_posters: Optional[Dict[str, str]] = None  # slug -> ua_poster_url

        # Temporary fields for load_details
        self.description: Optional[str] = None
        self.genres: List[str] = []
        self.watch_links: List[Dict[str, str]] = []

    def _extract_total_items(self, payload: dict) -> Optional[int]:
        pag = payload.get("pagination")
        if not isinstance(pag, dict):
            return None

        for key in ("total", "total_items", "totalItems", "count"):
            if key in pag:
                t = _to_int(pag.get(key))
                if t is not None and t > 0:
                    return t
        return None

    @property
    def headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def build_body_random(self, media_types: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "media_type": media_types or [],
            "status": [],
            "season": [],
            "rating": [],
            "years": [],
            "genres": [],
            "studios": [],
            "only_translated": True,
            "sort": ["score:desc"],
        }

    async def _resolve_url(self, session: aiohttp.ClientSession) -> str:
        if self._resolved_url:
            return self._resolved_url

        body = self.build_body_random()
        last: List[str] = []
        for pref in self.PREFIXES:
            url = f"{self.base_url}{pref}{self.PATH}"
            try:
                async with session.post(
                    url,
                    headers=self.headers,
                    params={"page": 1},
                    json=body,
                    timeout=20,
                ) as r:
                    txt = await r.text()
                    if r.status == 404:
                        last.append(f"404 {url}")
                        continue
                    if r.status in (401, 403):
                        self._resolved_url = url
                        return url
                    if r.status >= 400:
                        last.append(f"{r.status} {url}: {txt[:180]}")
                        continue
                    self._resolved_url = url
                    return url
            except Exception as e:
                last.append(f"EXC {url}: {e}")

        raise RuntimeError(
            "Не вдалося знайти робочий endpoint Hikka для POST /anime?page=...\n"
            + "\\n".join(last[:12])
        )

    def _extract_total_pages(self, payload: Dict[str, Any]) -> Optional[int]:
        pag = payload.get("pagination")
        if not isinstance(pag, dict):
            return None

        for key in ("total_pages", "totalPages", "pages", "pageCount"):
            if key in pag:
                tp = _to_int(pag.get(key))
                if tp and tp > 0:
                    return tp

        total = _to_int(pag.get("total"))
        per_page = _to_int(pag.get("per_page") or pag.get("perPage") or pag.get("size") or pag.get("limit"))
        if total and per_page and per_page > 0:
            return max(1, (total + per_page - 1) // per_page)

        return None

    def parse_list(self, payload: Dict[str, Any]) -> List[Anime]:
        raw_list = payload.get("list")
        if not isinstance(raw_list, list):
            raw_list = []

        items: List[Anime] = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue

            slug = str(it.get("slug") or "").strip()
            if not slug:
                continue

            title = (
                it.get("title_ua")
                or it.get("title_en")
                or it.get("title_ja")
                or it.get("title")
                or slug
            )

            score = _to_float(it.get("score"))
            year = _to_int(it.get("year"))
            episodes_total = _to_int(it.get("episodes_total"))
            poster_url = it.get("image")
            content_type = it.get("media_type")  # tv, movie, special, ova

            # Parse genres from base list
            genres: List[str] = []
            g = it.get("genres")
            if isinstance(g, list):
                for x in g:
                    if isinstance(x, str):
                        s = x.strip()
                        if s:
                            genres.append(s)
                    elif isinstance(x, dict):
                        name = x.get("name_ua") or x.get("name_en") or x.get("name") or x.get("slug")
                        if name:
                            s = str(name).strip()
                            if s:
                                genres.append(s)

            hikka_url = f"https://hikka.io/anime/{quote_plus(slug)}"

            items.append(
                Anime(
                    id=slug,
                    slug=slug,
                    title=str(title),
                    year=year,
                    score=score,
                    genres=genres[:8],
                    episodes_total=episodes_total,
                    description=None,  # Description loaded later
                    poster_url=str(poster_url) if poster_url else None,
                    hikka_url=hikka_url,
                    watch_links=[],
                    content_type=str(content_type) if content_type else None,
                )
            )
        return items

    async def fetch_page(self, session: aiohttp.ClientSession, *, page: int, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = await self._resolve_url(session)
        if body is None:
            body = self.build_body_random()
        async with session.post(
            url,
            headers=self.headers,
            params={"page": page},
            json=body,
            timeout=25,
        ) as r:
            txt = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Hikka error {r.status}: {txt[:400]}")
            return await r.json()

    async def get_total_pages(self, session: aiohttp.ClientSession, body: Optional[Dict[str, Any]] = None) -> int:
        is_filtered = body is not None and (body.get("genres") or body.get("years") or body.get("season"))
        
        if not is_filtered and self._total_pages and self._total_pages > 0:
            return self._total_pages

        payload = await self.fetch_page(session, page=1, body=body)
        tp = self._extract_total_pages(payload)
        
        result = tp if (tp and tp > 0) else 500
        
        if not is_filtered:
            self._total_pages = result
            
        return result

    async def fetch_anime_details(self, session: aiohttp.ClientSession, slug: str) -> Dict[str, Any]:
        url = await self._resolve_url(session)
        base = url.rsplit("/", 1)[0]

        alternatives = [
            f"{base}/anime/{slug}",
            f"{self.base_url}/anime/{slug}",
            f"{self.base_url}/v1/anime/{slug}",
            f"{self.base_url}/api/anime/{slug}",
            f"{self.base_url}/api/v1/anime/{slug}",
        ]

        for d_url in alternatives:
            try:
                async with session.get(d_url, headers=self.headers, timeout=20) as r:
                    if r.status == 200:
                        return await r.json()
                    print(f"[DETAILS] {d_url} → HTTP {r.status}")
            except Exception as e:
                print(f"[DETAILS] Помилка {d_url}: {e}")

        raise RuntimeError(f"Не вдалося знайти ендпоінт для деталей аніме {slug}")

    async def load_details(self, session: aiohttp.ClientSession, slug: str) -> None:
        try:
            data = await self.fetch_anime_details(session, slug)

            desc = data.get("synopsis_ua") or data.get("description_ua")
            self.description = clean_synopsis(str(desc)) if desc else None

            genres_raw = data.get("genres")
            if isinstance(genres_raw, list):
                self.genres = []
                for g in genres_raw:
                    if isinstance(g, str):
                        s = g.strip()
                        if s:
                            self.genres.append(s)
                    elif isinstance(g, dict):
                        name = g.get("name_ua") or g.get("name_en") or g.get("name") or g.get("slug")
                        if name:
                            s = str(name).strip()
                            if s:
                                self.genres.append(s)
                self.genres = self.genres[:8]
            else:
                self.genres = []

            self.watch_links = []
            external = data.get("external") or []
            if isinstance(external, list):
                for ext in external:
                    if isinstance(ext, dict) and ext.get("type") == "watch" and ext.get("url"):
                        self.watch_links.append({
                            "text": str(ext.get("text") or ext.get("url") or "Дивитись"),
                            "url": str(ext["url"])
                        })

            print(f"[DETAILS] {slug}: жанрів={len(self.genres)}, опис={'є' if self.description else 'немає'}, "
                  f"посилань={len(self.watch_links)}")

        except Exception as e:
            raise RuntimeError(f"Помилка завантаження деталей для {slug}: {e}")

    async def load_ua_posters(self, session: aiohttp.ClientSession) -> None:
        if self.ua_posters is not None:
            return

        self.ua_posters = {}
        url = "https://raw.githubusercontent.com/DrBryanMan/UAPosters/main/PostersList.json"

        try:
            async with session.get(url, timeout=30) as r:
                if r.status != 200:
                    return
                data = await r.json(content_type=None)

                if not isinstance(data, list):
                    return

                for item in data:
                    hikka_url_raw = item.get("hikka_url")
                    posters = item.get("posters")
                    if not hikka_url_raw or not posters or not isinstance(posters, list):
                        continue

                    slug = hikka_url_raw.split("/anime/")[-1].strip().rstrip("/")
                    first_poster = posters[0].get("url")
                    if first_poster:
                        rel_path = first_poster.strip()
                        if rel_path.startswith("/"):
                            rel_path = rel_path[1:]
                        safe_path = quote(rel_path, safe='/')
                        full_url = f"https://raw.githubusercontent.com/DrBryanMan/UAPosters/main/{safe_path}"
                        self.ua_posters[slug] = full_url

                print(f"[UA_POSTERS] Завантажено {len(self.ua_posters)} постерів")

        except Exception as e:
            print(f"[UA_POSTERS] Помилка: {e}")


    async def _fetch_ua_posters_map(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        """Fetches and builds a map of slug -> ua_poster_url"""
        POSTERS_JSON_URL = "https://raw.githubusercontent.com/DrBryanMan/UAPosters/main/PostersList.json"
        POSTERS_BASE_URL = "https://raw.githubusercontent.com/DrBryanMan/UAPosters/main/"
        
        try:
            print("[LIBRARY] 🔽 Завантаження мапи українських постерів...")
            async with session.get(POSTERS_JSON_URL) as resp:
                if resp.status != 200:
                    print(f"[LIBRARY] ❌ Помилка завантаження постерів: статус {resp.status}")
                    return {}
                data = await resp.json(content_type=None)
                
            ua_map = {}
            for item in data:
                hikka_url = item.get("hikka_url", "")
                posters = item.get("posters", [])
                
                if hikka_url and posters:
                    slug = hikka_url.rstrip("/").split("/")[-1]
                    
                    rel_path = posters[0].get("url")
                    if rel_path:
                        if rel_path.startswith("/"):
                            rel_path = rel_path[1:]
                        
                        # Use quote() to properly encode Cyrillic characters and spaces
                        safe_path = quote(rel_path, safe='/')
                        full_url = POSTERS_BASE_URL + safe_path
                        ua_map[slug] = full_url
            
            print(f"[LIBRARY] ✅ Завантажено {len(ua_map)} українських постерів")
            return ua_map
            
        except Exception as e:
            print(f"[LIBRARY] ⚠️ Помилка обробки мапи постерів: {e}")
            return {}

    async def _check_image_url(self, session: aiohttp.ClientSession, url: str) -> bool:
        """Checks if URL is accessible (HEAD request)"""
        try:
            async with session.head(url, timeout=5, allow_redirects=True) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _fetch_genres_from_api(self, session: aiohttp.ClientSession) -> Optional[List[Dict[str, str]]]:
        """Fetches genres list from API"""
        url = f"{self.base_url}/genres"
        try:
            async with session.get(url, headers=self.headers, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[GENRES] ❌ API помилка {resp.status}: {text[:200]}")
                    return None
                
                data = await resp.json()
                
                if isinstance(data, dict):
                    genres_list = data.get("list", [])
                elif isinstance(data, list):
                    genres_list = data
                else:
                    print(f"[GENRES] ❌ Невідомий формат відповіді: {type(data)}")
                    return None
                
                return genres_list
        except Exception as e:
            print(f"[GENRES] ❌ Виняток: {e}")
            return None

    async def sync_library(self, full: bool = False) -> None:
        """
        Фоновий процес: скачує всі сторінки аніме і зберігає в локальну БД.
        full=True - ігнорує дати та перезаписує.
        """
        # Перевірка часу останньої синхронізації
        if not full:
            # 1. Main Sync Check (13 days)
            cached_lib = meta_get(META_LAST_LIBRARY_SYNC)
            if cached_lib:
                val, updated_at = cached_lib
                if int(time.time()) - updated_at < LIBRARY_SYNC_INTERVAL:
                    print(f"[LIBRARY] ⏳ Синхронізація бібліотеки не потрібна (оновлено {int((int(time.time()) - updated_at)/3600)} год тому)")
                    return

            # 2. Poster Sync Check (14 days)
            cached_posters = meta_get(META_LAST_POSTERS_SYNC)
            need_poster_sync = True # Default for first run
            if cached_posters:
                val, updated_at = cached_posters
                if int(time.time()) - updated_at < POSTER_SYNC_INTERVAL:
                    need_poster_sync = False
        else:
            need_poster_sync = True

        poster_status = "ПОВНА (з перевіркою постерів)" if need_poster_sync else "ШВИДКА (без перевірки постерів)"
        print(f"[LIBRARY] 🚀 Починаю синхронізацію бібліотеки ({poster_status})...")
        start_ts = time.time()
        
        # Select query based on poster sync need
        insert_query = INSERT_LIBRARY_ITEM if need_poster_sync else INSERT_LIBRARY_ITEM_SKIP_POSTER_UPDATE

        async with aiohttp.ClientSession() as session:
            # === ШАГ 0: СИНХРОНИЗАЦИЯ ЖАНРОВ (ОПЦИОНАЛЬНАЯ) ===
            print(f"[LIBRARY] 📚 ШАГ 0/4: Синхронізація жанрів...")
            try:
                genres_data = await self._fetch_genres_from_api(session)
                if genres_data:
                    genres_json = json.dumps(genres_data, ensure_ascii=False)
                    meta_set(META_AVAILABLE_GENRES, genres_json)
                    
                    # Синхронізуємо GENRE_MAP у пам'яті
                    from genre_filters import sync_genre_mapping
                    sync_genre_mapping(genres_data)
                    
                    print(f"[LIBRARY] ✅ Жанри синхронізовано: {len(genres_data)} записів")
                else:
                    print(f"[LIBRARY] ⚠️ Не вдалося синхронізувати жанри, продовжую...")
            except Exception as e:
                print(f"[LIBRARY] ⚠️ Ошибка синхронізації жанрів: {e}, продовжую...")
            
            # === ЕТАП 1: Завантаження всіх аніме (РОЗУМНА СИНХРОНІЗАЦІЯ) ===
            print(f"[LIBRARY] 📥 ЕТАП 1/4: Розумна синхронізація...")
            # 1. Отримуємо загальну кількість сторінок
            try:
                total_pages = await self.get_total_pages(session)
                print(f"[LIBRARY] Знайдено {total_pages} сторінок")
            except Exception as e:
                print(f"[LIBRARY] ❌ Помилка отримання кількості сторінок: {e}")
                return
            conn = db()
            total_items = 0
            skipped = 0
            updated = 0
            inserted = 0
            now = int(time.time())
            
            for page in range(1, total_pages + 1):
                try:
                    payload = await self.fetch_page(session, page=page)
                    items = self.parse_list(payload)
                    
                    if not items:
                        continue
                    
                    # Отримуємо slug-и зі сторінки
                    slugs = [anime.slug for anime in items]
                    
                    # Завантажуємо існуючі записи з бази одним запитом
                    placeholders = ','.join('%s' for _ in slugs)
                    existing_rows = conn.execute(
                        f"SELECT slug, title, genres_json, score, year, episodes_total, poster_url FROM anime_library WHERE slug IN ({placeholders})",
                        slugs
                    ).fetchall()
                    
                    # Створюємо словник існуючих записів
                    existing = {row[0]: row for row in existing_rows}
                    
                    to_insert = []
                    to_update = []
                    
                    for anime in items:
                        db_record = existing.get(anime.slug)
                        api_genres = json.dumps(anime.genres, ensure_ascii=False) if anime.genres else '[]'
                        
                        if db_record is None:
                            # Нове аніме - вставляємо
                            to_insert.append((
                                anime.slug, anime.title, api_genres,
                                anime.score, anime.year, anime.episodes_total,
                                anime.poster_url, anime.hikka_url, now, None, anime.content_type
                            ))
                        else:
                            # Порівнюємо з існуючим записом
                            db_slug, db_title, db_genres, db_score, db_year, db_episodes, db_poster = db_record
                            
                            # Перевірка чи потрібно оновлення
                            needs_update = False
                            new_title = db_title
                            new_genres = db_genres
                            new_score = db_score
                            new_year = db_year
                            new_episodes = db_episodes
                            new_poster = db_poster
                            
                            # Оновлюємо title якщо змінився
                            if anime.title and anime.title != db_title:
                                new_title = anime.title
                                needs_update = True
                            
                            # Оновлюємо genres ТІЛЬКИ якщо в базі порожньо
                            if (not db_genres or db_genres == '[]') and anime.genres:
                                new_genres = api_genres
                                needs_update = True
                            
                            # Оновлюємо score якщо в базі немає
                            if db_score is None and anime.score is not None:
                                new_score = anime.score
                                needs_update = True
                            
                            # Оновлюємо year якщо в базі немає
                            if db_year is None and anime.year is not None:
                                new_year = anime.year
                                needs_update = True
                            
                            # Оновлюємо episodes якщо в базі немає
                            if db_episodes is None and anime.episodes_total is not None:
                                new_episodes = anime.episodes_total
                                needs_update = True
                            
                            # Оновлюємо poster якщо в базі немає
                            if not db_poster and anime.poster_url:
                                new_poster = anime.poster_url
                                needs_update = True
                            
                            if needs_update:
                                to_update.append((
                                    new_title, new_genres, new_score, new_year,
                                    new_episodes, new_poster, now, anime.content_type, anime.slug
                                ))
                            else:
                                skipped += 1
                    
                    # Виконуємо batch операції
                    with transaction():
                        if to_insert:
                            conn.executemany(INSERT_LIBRARY_ITEM_SKIP_POSTER_UPDATE, to_insert)
                            inserted += len(to_insert)
                        
                        if to_update:
                            conn.executemany(
                                """UPDATE anime_library SET 
                                   title=%s, genres_json=%s, score=%s, year=%s, 
                                   episodes_total=%s, poster_url=%s, updated_at=%s, content_type=%s
                                   WHERE slug=%s""",
                                to_update
                            )
                            updated += len(to_update)
                    
                    total_items += len(items)
                    
                    if page % 10 == 0:
                        print(f"[LIBRARY] Сторінок: {page}/{total_pages} | Нових: {inserted} | Оновлено: {updated} | Пропущено: {skipped}")
                    
                    await asyncio.sleep(0.3)  # Швидше бо менше записів
                    
                except Exception as e:
                    print(f"[LIBRARY] ⚠️ Помилка на сторінці {page}: {e}")
                    await asyncio.sleep(2)
            
            print(f"[LIBRARY] ✅ ЕТАП 1 завершено: нових {inserted}, оновлено {updated}, пропущено {skipped}")
            
            # === ЕТАП 1.5: Заповнення порожніх жанрів ===
            print(f"[LIBRARY] 📚 ЕТАП 1.5/4: Заповнення порожніх жанрів...")
            try:
                genres_updated, genres_failed = await self._fill_missing_genres(session)
                print(f"[LIBRARY] ✅ ЕТАП 1.5 завершено: оновлено жанрів для {genres_updated} аніме")
            except Exception as e:
                print(f"[LIBRARY] ⚠️ Помилка заповнення жанрів: {e}, продовжую...")
            
            # === ЕТАП 2: Завантаження та валідація UA постерів (якщо потрібно) ===
            if need_poster_sync:
                print(f"[LIBRARY] 🎨 ЕТАП 2/4: Завантаження та перевірка українських постерів...")
                ua_posters = await self._fetch_ua_posters_map(session)
                
                if ua_posters:
                    # Завантажуємо існуючі постери з бази одним запитом
                    existing_posters = conn.execute(
                        "SELECT slug FROM anime_library WHERE ua_poster_url IS NOT NULL AND ua_poster_url != ''"
                    ).fetchall()
                    existing_slugs = {row[0] for row in existing_posters}
                    
                    # Фільтруємо постери - залишаємо тільки ті, яких немає в базі
                    posters_to_check = {slug: url for slug, url in ua_posters.items() if slug not in existing_slugs}
                    skipped_count = len(ua_posters) - len(posters_to_check)
                    
                    if skipped_count > 0:
                        print(f"[LIBRARY] Пропущено {skipped_count} постерів (вже є в базі)")
                    
                    validated_count = 0
                    failed_count = 0
                    
                    for slug, ua_url in posters_to_check.items():
                        # Перевіряємо валідність постера
                        is_valid = await self._check_image_url(session, ua_url)
                        
                        if is_valid:
                            # Оновлюємо аніме в базі
                            with transaction():
                                conn.execute(
                                    "UPDATE anime_library SET ua_poster_url = %s WHERE slug = %s",
                                    (ua_url, slug)
                                )
                            validated_count += 1
                        else:
                            failed_count += 1
                            print(f"[LIBRARY] ⚠️ Битий постер для {slug}")
                        
                        # Progress кожні 100 постерів
                        if (validated_count + failed_count) % 100 == 0:
                            print(f"[LIBRARY] Перевірено постерів: {validated_count + failed_count}/{len(posters_to_check)}")
                        
                        await asyncio.sleep(0.1)  # Невелика затримка між перевірками
                    
                    print(f"[LIBRARY] ✅ ЕТАП 2 завершено: {validated_count} валідних, {failed_count} битих, {skipped_count} пропущено")
                else:
                    print(f"[LIBRARY] ⚠️ Не вдалося завантажити мапу постерів")
            else:
                print(f"[LIBRARY] ⏭️ ЕТАП 2 пропущено (швидка синхронізація)")
            
            duration = time.time() - start_ts
            
            # Отримуємо реальну кількість записів у базі після синхронізації
            count_row = conn.execute(COUNT_LIBRARY).fetchone()
            actual_count = count_row[0] if count_row else 0
            
            print(f"[LIBRARY] ✅ Синхронізацію завершено за {duration:.1f}с. Всього аніме в базі: {actual_count}")
            meta_set(META_LAST_LIBRARY_SYNC, "done")
            if need_poster_sync:
                meta_set(META_LAST_POSTERS_SYNC, "done")
            
            # Update total translated count - використовуємо реальну кількість з бази
            meta_set(META_TOTAL_TRANSLATED, str(actual_count))

    async def random_anime(
        self,
        exclude_ids: List[str],
        last_page: Optional[int] = None,
        genres: Optional[List[str]] = None,
        genre_names: Optional[List[str]] = None,
        excluded_genres: Optional[List[str]] = None,
        content_types: Optional[List[str]] = None,
        excluded_content_types: Optional[List[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        tries: int = 12,
    ) -> Tuple[Anime, int]:
        conn = db()
        
        # 1. Перевірка наявності бібліотеки
        count_row = conn.execute(COUNT_LIBRARY).fetchone()
        count = count_row[0] if count_row else 0
        
        if count < 100:
            print(f"[RANDOM] ⚠️ Бібліотека порожня ({count} items), використовую HTTP-метод")
            return await self._random_anime_http(exclude_ids, last_page, genres, excluded_genres, content_types, excluded_content_types, year_from, year_to, tries)
        
        # 2. Якщо є фільтри по жанрах - перевіряємо, чи достатньо жанрів у базі
        if genres or excluded_genres:
            # Підраховуємо, скільки аніме мають жанри
            genres_count = conn.execute(
                "SELECT COUNT(*) FROM anime_library WHERE genres_json != '[]' AND genres_json IS NOT NULL"
            ).fetchone()[0]
            
            if genres_count < 100:  # Мало даних для фільтрації
                print(f"[RANDOM] 🔄 Жанрів у базі мало ({genres_count}), використовую HTTP API")
                return await self._random_anime_http(exclude_ids, last_page, genres, excluded_genres, content_types, excluded_content_types, year_from, year_to, tries)
            else:
                print(f"[RANDOM] ⚡ Жанрів у базі достатньо ({genres_count}), пробую SQL пошук")
        
        # 3. SQL Пошук
        print(f"[RANDOM] ⚡ Шукаю в локальній бібліотеці (Total: {count})")
        
        query = "SELECT slug, title, genres_json, score, year, episodes_total, poster_url, hikka_url, ua_poster_url, content_type FROM anime_library"
        params = []
        conditions = []
        
        # Фільтр: виключаємо аніме з рейтингом 0 або NULL
        conditions.append("score IS NOT NULL AND score > 0")
        
        if exclude_ids:
            placeholders = ','.join('%s' for _ in exclude_ids)
            conditions.append(f"slug NOT IN ({placeholders})")
            params.extend(exclude_ids)
                
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        # Отримуємо всі записи з бази
        rows = conn.execute(query, params).fetchall()
        
        # ✅ Виносимо конвертацію жанрів перед циклом (performance optimization)
        excluded_names = []
        if excluded_genres:
            excluded_names = [get_name_by_slug(s) for s in excluded_genres]
            
        # Фільтрація Python (exclude_ids + genres + content_types)
        candidates = []
        ex_set = set(exclude_ids)
        
        for r in rows:
            slug, title, g_json, sc, yr, ep, poster, h_url, ua_poster, ctype = r
            if slug in ex_set:
                continue
            
            # Парсинг жанрів для об'єкта
            try:
                g_list = json.loads(g_json)
            except:
                g_list = []
            
            # Фільтрація по жанрах (якщо активна)
            if genres or excluded_genres:
                # Для фільтрації використовуємо genre_names (українські назви)
                if genre_names:
                    # Всі вибрані жанри мають бути присутні
                    if not set(genre_names).issubset(set(g_list)):
                        continue
                
                # ✅ Використовуємо вже конвертовані excluded_names
                if excluded_names:
                    # Жоден з виключених не має бути
                    if not set(excluded_names).isdisjoint(set(g_list)):
                        continue

            if content_types or excluded_content_types:
                # Якщо вказані конкретні типи - аніме має бути одним з них
                if content_types and ctype not in content_types:
                    continue
                
                # Якщо вказані виключені типи - аніме не має бути одним з них
                if excluded_content_types and ctype in excluded_content_types:
                    continue
            
            # Фільтрація по рокам
            if year_from or year_to:
                if yr is None:
                    continue
                if year_from and yr < year_from:
                    continue
                if year_to and yr > year_to:
                    continue
            
            # Фільтр: виключаємо аніме з рейтингом 0 або NULL
            if sc is None or sc <= 0:
                continue

            candidates.append(Anime(
                id=slug, slug=slug, title=title,
                year=yr, score=sc, genres=g_list,
                episodes_total=ep, poster_url=poster,
                hikka_url=h_url, description=None, watch_links=[],
                ua_poster_url=ua_poster, content_type=ctype
            ))
            
        if not candidates:
            # 🔄 SMART FALLBACK: Якщо точний пошук по всіх жанрах не дав результату
            if genre_names and len(genre_names) > 1:
                print(f"[RANDOM] ⚠️ SQL не знайшов для всіх жанрів: {genre_names}. Вмикаю Smart Fallback...")
                
                # Перемішуємо жанри для різноманітності
                shuffled_names = list(genre_names)
                random.shuffle(shuffled_names)
                
                for single_genre in shuffled_names:
                    print(f"[RANDOM] 🔄 Пробую окремий жанр в БД: {single_genre}")
                    
                    # Пошук по одному жанру
                    for r in rows:
                        slug, title, g_json, sc, yr, ep, poster, h_url, ua_poster, ctype = r
                        if slug in ex_set:
                            continue
                        
                        try:
                            g_list = json.loads(g_json) if g_json else []
                        except:
                            g_list = []
                        
                        # Перевіряємо чи є ХОЧА Б цей один жанр
                        if single_genre not in g_list:
                            continue
                        
                        # Перевіряємо excluded жанри
                        if excluded_names:
                            if not set(excluded_names).isdisjoint(set(g_list)):
                                continue
                        
                        # ✅ Фільтрація по типах контенту (SMART FALLBACK)
                        if content_types or excluded_content_types:
                            if content_types and ctype not in content_types:
                                continue
                            if excluded_content_types and ctype in excluded_content_types:
                                continue
                        
                        # ✅ Фільтрація по роках (SMART FALLBACK)
                        if year_from or year_to:
                            if yr is None:
                                continue
                            if year_from and yr < year_from:
                                continue
                            if year_to and yr > year_to:
                                continue
                        
                        # Фільтр: виключаємо аніме з рейтингом 0 або NULL
                        if sc is None or sc <= 0:
                            continue
                        
                        candidates.append(Anime(
                            id=slug, slug=slug, title=title,
                            year=yr, score=sc, genres=g_list,
                            episodes_total=ep, poster_url=poster,
                            hikka_url=h_url, description=None, watch_links=[],
                            ua_poster_url=ua_poster, content_type=ctype
                        ))
                    
                    if candidates:
                        print(f"[RANDOM] ✅ Smart Fallback знайшов {len(candidates)} аніме в жанрі '{single_genre}'")
                        break
            
            # Якщо і Smart Fallback не допоміг - HTTP
            if not candidates:
                if genres or excluded_genres or content_types or excluded_content_types or year_from or year_to:
                    print(f"[RANDOM] ❌ SQL + Smart Fallback не знайшли кандидатів, fallback на HTTP API")
                    return await self._random_anime_http(exclude_ids, last_page, genres, excluded_genres, content_types, excluded_content_types, year_from, year_to, tries)
                else:
                    raise AllAnimeSeenError(count)
            
        got = random.choice(candidates)
        print(f"[RANDOM] ✅ Знайдено в SQL: {got.title}")
        
        # Відновлюємо деталі (опис, лінки) через старий механізм (кеш або запит)
        async with aiohttp.ClientSession() as session:
             await self._enrich_anime_details(session, got)
             return got, 1

    async def _enrich_anime_details(self, session: aiohttp.ClientSession, got: Anime):
        """Підтягує деталі (опис, лінки) + LAZY LOADING + створення в anime_library"""
        
        # Перевіряємо чи є жанри в anime_library (для lazy loading)
        library_has_genres = self._check_library_has_genres(got.slug)
        library_record_exists = self._library_record_exists(got.slug)
        
        # Кеш тепер зберігає тільки опис і лінки (не жанри)
        cached = get_cached_details(got.slug)
        if cached:
            cached_desc, cached_links = cached
            if not got.description and cached_desc:
                got.description = cached_desc
            if not got.watch_links and cached_links:
                got.watch_links = cached_links
            
        # Якщо немає опису/лінків/жанрів — йдемо на API
        if not got.description or not got.watch_links or not got.genres:
            try:
                await self.load_details(session, got.slug)
                got.description = self.description or got.description
                got.watch_links = self.watch_links or got.watch_links
                got.genres = self.genres or got.genres
                
                # Зберігаємо тільки опис і лінки в кеш
                set_cached_details(got.slug, got.description, got.watch_links)
                    
            except Exception as e:
                print(f"[ENRICH] Error loading details: {e}")
        
        # LAZY: Створюємо/оновлюємо запис в anime_library якщо потрібно
        if not library_record_exists or (got.genres and not library_has_genres):
            self._ensure_anime_in_library(got)

    def _check_library_has_genres(self, slug: str) -> bool:
        """Перевіряє чи є жанри в anime_library для цього slug"""
        try:
            conn = db()
            row = conn.execute(
                "SELECT genres_json FROM anime_library WHERE slug=%s", (slug,)
            ).fetchone()
            if row and row[0]:
                # Є запис і genres_json не пустий
                genres = json.loads(row[0])
                return isinstance(genres, list) and len(genres) > 0
            return False
        except Exception:
            return False

    def _library_record_exists(self, slug: str) -> bool:
        """Перевіряє чи існує запис в anime_library для цього slug"""
        try:
            conn = db()
            row = conn.execute(
                "SELECT 1 FROM anime_library WHERE slug=%s", (slug,)
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _ensure_anime_in_library(self, anime: Anime) -> None:
        """
        Забезпечує що аніме є в anime_library.
        Якщо запис існує - оновлює жанри.
        Якщо запису немає - створює новий.
        """
        if not anime or not anime.slug:
            return
        
        try:
            genres_json = json.dumps(anime.genres[:8] if anime.genres else [], ensure_ascii=False)
            now = int(time.time())
            
            with transaction():
                conn = db()
                
                # Перевіряємо чи існує запис
                if self._library_record_exists(anime.slug):
                    # Оновлюємо тільки жанри якщо запис існує
                    conn.execute(
                        UPDATE_LIBRARY_GENRES,
                        (genres_json, now, anime.slug)
                    )
                    print(f"[LAZY] ✅ Оновлено жанри для {anime.slug}: {anime.genres[:3]}...")
                else:
                    # Створюємо новий запис
                    conn.execute(
                        INSERT_LIBRARY_ITEM_SKIP_POSTER_UPDATE,
                        (
                            anime.slug,
                            anime.title,
                            genres_json,
                            anime.score,
                            anime.year,
                            anime.episodes_total,
                            anime.poster_url,
                            anime.hikka_url,
                            now,
                            anime.ua_poster_url,
                            anime.content_type
                        )
                    )
                    print(f"[LAZY] ✅ Створено запис в library для {anime.slug}: '{anime.title}'")
                    
        except Exception as e:
            print(f"[LAZY] ⚠️ Помилка збереження {anime.slug}: {e}")

    def _parse_genres_from_details(self, data: Dict[str, Any]) -> List[str]:
        """Витягнути список жанрів з відповіді API деталей"""
        genres_raw = data.get("genres")
        if not isinstance(genres_raw, list):
            return []
        
        genres = []
        for g in genres_raw:
            if isinstance(g, str):
                s = g.strip()
                if s:
                    genres.append(s)
            elif isinstance(g, dict):
                name = g.get("name_ua") or g.get("name_en") or g.get("name") or g.get("slug")
                if name:
                    s = str(name).strip()
                    if s:
                        genres.append(s)
        return genres[:8]

    async def _fill_missing_genres(self, session: aiohttp.ClientSession, limit: Optional[int] = None) -> Tuple[int, int]:
        """
        Заповнює порожні жанри для аніме, які їх не мають.
        Повертає (updated_count, failed_count)
        """
        conn = db()
        
        # Знайти аніме без жанрів
        query = """
            SELECT slug, title FROM anime_library 
            WHERE genres_json IS NULL 
               OR genres_json = '[]' 
               OR genres_json = ''
            ORDER BY updated_at DESC
        """
        if limit:
            query += " LIMIT %s"
            rows = conn.execute(query, (limit,)).fetchall()
        else:
            rows = conn.execute(query).fetchall()
        total = len(rows)
        
        if total == 0:
            print(f"[GENRES] ✅ Всі аніме вже мають жанри!")
            return 0, 0
        
        print(f"[GENRES] 📚 Знайдено {total} аніме без жанрів, починаю заповнення...")
        
        updated = 0
        failed = 0
        
        for i, (slug, title) in enumerate(rows, 1):
            try:
                # Завантажуємо деталі через API
                data = await self.fetch_anime_details(session, slug)
                if not data:
                    failed += 1
                    if i % 50 == 0:
                        print(f"[GENRES] Прогрес: {i}/{total} | Оновлено: {updated} | Помилок: {failed}")
                    await asyncio.sleep(0.3)
                    continue
                
                # Парсимо жанри
                genres = self._parse_genres_from_details(data)
                if not genres:
                    failed += 1
                    if i % 50 == 0:
                        print(f"[GENRES] Прогрес: {i}/{total} | Оновлено: {updated} | Помилок: {failed}")
                    await asyncio.sleep(0.3)
                    continue
                
                # Оновлюємо в БД
                genres_json = json.dumps(genres, ensure_ascii=False)
                with transaction():
                    conn.execute(
                        UPDATE_LIBRARY_GENRES,
                        (genres_json, int(time.time()), slug)
                    )
                
                updated += 1
                
                # Прогрес кожні 50 аніме
                if i % 50 == 0:
                    print(f"[GENRES] Прогрес: {i}/{total} | Оновлено: {updated} | Помилок: {failed}")
                
                # Пауза щоб не перевантажувати API
                await asyncio.sleep(0.3)
                
            except Exception as e:
                failed += 1
                if i % 50 == 0 or failed <= 5:
                    print(f"[GENRES] ⚠️ Помилка для {slug}: {e}")
                await asyncio.sleep(0.3)
        
        print(f"[GENRES] ✅ Заповнення завершено: оновлено {updated}, помилок {failed}")
        return updated, failed

    async def _random_anime_http(
        self,
        exclude_ids: List[str],
        last_page: Optional[int] = None,
        genres: Optional[List[str]] = None,
        excluded_genres: Optional[List[str]] = None,
        content_types: Optional[List[str]] = None,
        excluded_content_types: Optional[List[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        tries: int = 12,
    ) -> Tuple[Anime, int]:
        """Старий метод пошуку через HTTP (збережений як фолбек)"""

        exclude = set(exclude_ids)
        ex_genres = set(excluded_genres) if excluded_genres else set()

        async def _search_attempt(
            session: aiohttp.ClientSession,
            search_genres: Optional[List[str]],
            attempt_tries: int
        ) -> Optional[Tuple[Anime, int]]:
            
            body = self.build_body_random(media_types=content_types)
            if search_genres:
                body["genres"] = search_genres
                print(f"[RANDOM] 🔍 Пошук за жанрами: {', '.join(search_genres)}")
            if content_types:
                print(f"[RANDOM] 🎬 Пошук за типами: {', '.join(content_types)}")
            if not search_genres and not content_types:
                print(f"[RANDOM] 🔍 Пошук без фільтрів")

            total_pages = await self.get_total_pages(session, body=body)
            print(f"[RANDOM] Знайдено {total_pages} сторінок")
            
            if total_pages == 0:
                return None

            for attempt in range(attempt_tries):
                page = 1 if total_pages <= 1 else random.randint(1, total_pages)
                if last_page is not None and page == last_page and total_pages > 1:
                    page = (page % total_pages) + 1

                try:
                    payload = await self.fetch_page(session, page=page, body=body)
                    
                    candidates = []
                    for a in self.parse_list(payload):
                        if a.id in exclude:
                            continue
                        candidates.append(a)
                        
                    if candidates and ex_genres:
                        filtered = []
                        # Convert excluded genre slugs to names once before loop
                        ex_genre_names = {get_name_by_slug(s) for s in ex_genres}
                        
                        for cand in candidates:
                            has_bad = any(gname in ex_genre_names for gname in cand.genres)
                            if not has_bad:
                                filtered.append(cand)
                        candidates = filtered
                    
                    # Filter by excluded content types
                    if candidates and excluded_content_types:
                        candidates = [c for c in candidates if c.content_type not in excluded_content_types]
                    
                    # Filter by year range
                    if candidates and (year_from or year_to):
                        filtered = []
                        for c in candidates:
                            if c.year is None:
                                continue
                            if year_from and c.year < year_from:
                                continue
                            if year_to and c.year > year_to:
                                continue
                            filtered.append(c)
                        candidates = filtered
                    
                    # Фільтр: виключаємо аніме з рейтингом 0 або NULL
                    if candidates:
                        candidates = [c for c in candidates if c.score is not None and c.score > 0]

                    if candidates:
                        return random.choice(candidates), page
                except Exception as e:
                    print(f"[RANDOM] Помилка отримання сторінки {page}: {e}")
            
            return None

        async with aiohttp.ClientSession() as session:
            # 1. STRICT SEARCH
            result = await _search_attempt(session, genres, tries)
            
            if result:
                got, page = result
            
            # 2. SMART FALLBACK
            elif genres and len(genres) > 1:
                print(f"[RANDOM] ⚠️ Нічого не знайдено для {genres}. Вмикаю 'Smart Fallback'...")
                
                shuffled_genres = list(genres)
                random.shuffle(shuffled_genres)
                
                fallback_found = False
                last_total_pages = None
                
                for g in shuffled_genres:
                    print(f"[RANDOM] 🔄 Пробую окремий жанр: {g}")
                    result = await _search_attempt(session, [g], 3)
                    
                    if result:
                        got, page = result
                        print(f"[RANDOM] ✅ Знайдено в жанрі '{g}'!")
                        fallback_found = True
                        break
                    else:
                        body = self.build_body_random()
                        body["genres"] = [g]
                        last_total_pages = await self.get_total_pages(session, body=body)
                
                if not fallback_found:
                    total_pages_msg = f" (доступно сторінок: {last_total_pages})" if last_total_pages else ""
                    raise RuntimeError(f"Нічого не знайдено навіть по окремих жанрах 😔{total_pages_msg}")
            
            else:
                body = self.build_body_random()
                if genres:
                    body["genres"] = genres
                total_pages = await self.get_total_pages(session, body=body)
                
                msg = "Не знайшов що порекомендувати після кількох спроб."
                if total_pages == 1:
                    msg += " знайдено всього 1 сторінку перекладених тайтлів — можливо, ви вже все бачили?"
                else:
                     msg += f" (перевірено сторінок з {total_pages})"
                raise RuntimeError(msg)

            print(f"[РЕКОМЕНДАЦІЯ] Обрано: {got.slug} | {got.title}")

            cached = get_cached_details(got.slug)
            if cached:
                cached_desc, cached_links = cached
                if not got.description and cached_desc:
                    got.description = cached_desc
                if not got.watch_links and cached_links:
                    got.watch_links = cached_links

            if not got.genres:
                details_success = False
                for detail_attempt in range(3):
                    try:
                        await self.load_details(session, got.slug)
                        got.description = self.description or got.description
                        got.genres = self.genres or got.genres
                        got.watch_links = self.watch_links or got.watch_links

                        if got.genres:
                            details_success = True
                            # 🔥 LAZY LOADING: Зберігаємо/створюємо запис в бібліотеці
                            self._ensure_anime_in_library(got)

                        set_cached_details(
                            got.slug,
                            got.description,
                            got.watch_links
                        )
                        break

                    except Exception as e:
                        print(f"  → Спроба {detail_attempt + 1}/3 деталей провалилась: {e}")
                        if detail_attempt < 2:
                            await asyncio.sleep(1)

                if not details_success:
                    print(f"[WARNING] Не вдалося завантажити жанри для {got.slug}")
            return got, page
