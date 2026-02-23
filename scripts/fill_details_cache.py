"""
Одноразовий скрипт для заповнення таблиці anime_details_cache
даними тайтлів, які там відсутні.

Запуск: python fill_details_cache.py
"""
import aiohttp
import asyncio
import json
import time
import re
from database.connection import db, transaction

# Константи
HIKKA_BASE_URL = "https://api.hikka.io"  # Правильний API URL
DETAILS_TTL_SECONDS = 12 * 24 * 3600  # 12 днів

# Regex для обрізання хешу зі slug'а (формат: slug-name-abc123 -> slug-name)
_HASH_SUFFIX = re.compile(r"-[a-f0-9]{6}$")

# Regex для очистки опису
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SOURCE_LINE = re.compile(r"\n?\s*джерело.*$", re.IGNORECASE | re.DOTALL)
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC = re.compile(r"__([^_]+)__")


def clean_synopsis(text: str) -> str:
    """Очищує опис від посилань та конвертує Markdown -> HTML"""
    if not isinstance(text, str):
        return ""

    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _SOURCE_LINE.sub("", text)
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC.sub(r"<i>\1</i>", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s{2,}", " ", text).strip()

    return text


def get_missing_slugs() -> list[str]:
    """Отримує slug'и з anime_library, яких немає в anime_details_cache"""
    conn = db()
    rows = conn.execute("""
        SELECT l.slug 
        FROM anime_library l
        LEFT JOIN anime_details_cache c ON l.slug = c.slug
        WHERE c.slug IS NULL
        ORDER BY l.slug
    """).fetchall()
    return [row[0] for row in rows]


async def fetch_anime_details(session: aiohttp.ClientSession, slug: str) -> dict:
    """Завантажує деталі аніме з Hikka API"""
    url = f"{HIKKA_BASE_URL}/anime/{slug}"
    
    async with session.get(url, timeout=20) as r:
        if r.status == 200:
            return await r.json()
        raise RuntimeError(f"HTTP {r.status} для {slug}")


def save_to_cache(slug: str, description: str | None, watch_links: list[dict]) -> None:
    """Зберігає деталі в кеш"""
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


async def process_anime(session: aiohttp.ClientSession, slug: str) -> tuple[bool, str]:
    """Обробляє один тайтл, повертає (success, message)"""
    try:
        data = await fetch_anime_details(session, slug)
        
        # Опис
        desc = data.get("synopsis_ua") or data.get("description_ua")
        description = clean_synopsis(str(desc)) if desc else None
        
        # Watch links
        watch_links = []
        external = data.get("external") or []
        if isinstance(external, list):
            for ext in external:
                if isinstance(ext, dict) and ext.get("type") == "watch" and ext.get("url"):
                    watch_links.append({
                        "text": str(ext.get("text") or ext.get("url") or "Дивитись"),
                        "url": str(ext["url"])
                    })
        
        # Зберігаємо
        save_to_cache(slug, description, watch_links)
        
        return True, f"OK (опис: {'є' if description else 'немає'}, посилань: {len(watch_links)})"
        
    except Exception as e:
        return False, str(e)


async def main():
    print("=" * 60)
    print("  ЗАПОВНЕННЯ ANIME_DETAILS_CACHE")
    print("=" * 60)
    print()
    
    # Отримуємо slug'и
    missing_slugs = get_missing_slugs()
    total = len(missing_slugs)
    
    if total == 0:
        print("✅ Всі тайтли вже є в кеші! Нічого робити.")
        return
    
    print(f"📊 Знайдено {total} тайтлів без кешованих деталей")
    print()
    
    success_count = 0
    fail_count = 0
    start_time = time.time()
    
    async with aiohttp.ClientSession() as session:
        for i, slug in enumerate(missing_slugs, 1):
            success, message = await process_anime(session, slug)
            
            if success:
                success_count += 1
                status = "✅"
            else:
                fail_count += 1
                status = "❌"
            
            # Прогрес кожні 10 записів або помилка
            if i % 10 == 0 or not success or i == total:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                print(f"[{i}/{total}] {status} {slug}: {message}")
                if i % 50 == 0:
                    print(f"    📈 Швидкість: {rate:.1f}/сек, ETA: {eta/60:.1f} хв")
            
            # Затримка щоб не забанили
            await asyncio.sleep(0.5)
    
    # Підсумок
    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print("  РЕЗУЛЬТАТ")
    print("=" * 60)
    print(f"  ✅ Успішно: {success_count}")
    print(f"  ❌ Помилок: {fail_count}")
    print(f"  ⏱️  Час: {elapsed/60:.1f} хвилин")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
