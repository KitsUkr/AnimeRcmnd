"""
Скрипт для заповнення жанрів у anime_library.
Завантажує жанри з Hikka API для аніме, які не мають жанрів.
"""
import sqlite3
import aiohttp
import asyncio
import json
import time

DB_PATH = "bot.sqlite3"
HIKKA_BASE_URL = "https://api.hikka.io"

async def fetch_anime_details(session: aiohttp.ClientSession, slug: str) -> dict:
    """Отримати деталі аніме з Hikka API"""
    url = f"{HIKKA_BASE_URL}/anime/{slug}"
    try:
        async with session.get(url, timeout=20) as resp:
            if resp.status == 200:
                return await resp.json()
            print(f"  [ERROR] {slug}: HTTP {resp.status}")
            return {}
    except Exception as e:
        print(f"  [ERROR] {slug}: {e}")
        return {}

def parse_genres(data: dict) -> list:
    """Витягнути список жанрів з відповіді API"""
    genres_raw = data.get("genres", [])
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

async def fill_genres():
    """Головна функція заповнення жанрів"""
    conn = sqlite3.connect(DB_PATH)
    
    # Знайти аніме без жанрів
    rows = conn.execute("""
        SELECT slug, title FROM anime_library 
        WHERE genres_json IS NULL 
           OR genres_json = '[]' 
           OR genres_json = ''
    """).fetchall()
    
    total = len(rows)
    print(f"Знайдено {total} аніме без жанрів")
    
    if total == 0:
        print("Всі аніме вже мають жанри!")
        conn.close()
        return
    
    updated = 0
    failed = 0
    
    async with aiohttp.ClientSession() as session:
        for i, (slug, title) in enumerate(rows, 1):
            print(f"[{i}/{total}] {title or slug}...", end=" ")
            
            data = await fetch_anime_details(session, slug)
            if not data:
                failed += 1
                continue
            
            genres = parse_genres(data)
            if not genres:
                print("жанрів не знайдено")
                failed += 1
                continue
            
            # Оновити в БД
            genres_json = json.dumps(genres, ensure_ascii=False)
            conn.execute(
                "UPDATE anime_library SET genres_json = ?, updated_at = ? WHERE slug = ?",
                (genres_json, int(time.time()), slug)
            )
            conn.commit()
            
            print(f"OK ({len(genres)} жанрів)")
            updated += 1
            
            # Пауза щоб не перевантажувати API
            await asyncio.sleep(0.3)
    
    conn.close()
    
    print("\n" + "="*50)
    print(f"Готово!")
    print(f"  Оновлено: {updated}")
    print(f"  Помилок: {failed}")
    print(f"  Всього: {total}")

if __name__ == "__main__":
    print("="*50)
    print("  Заповнення жанрів у anime_library")
    print("="*50 + "\n")
    
    asyncio.run(fill_genres())
