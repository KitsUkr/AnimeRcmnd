"""
Скрипт для заповнення порожніх content_type в anime_library.
Використовує API Hikka для отримання media_type для кожного аніме.
"""

import aiohttp
import asyncio
import time
from database.connection import db, transaction


HIKKA_API_BASE = "https://api.hikka.io"


async def fetch_anime_info(session: aiohttp.ClientSession, slug: str) -> dict | None:
    """Отримує інформацію про аніме з API Hikka"""
    url = f"{HIKKA_API_BASE}/anime/{slug}"
    
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 404:
                print(f"[SKIP] {slug} - не знайдено (404)")
                return None
            else:
                print(f"[ERROR] {slug} - статус {resp.status}")
                return None
    except asyncio.TimeoutError:
        print(f"[TIMEOUT] {slug}")
        return None
    except Exception as e:
        print(f"[ERROR] {slug}: {e}")
        return None


async def fill_content_types():
    """Заповнює порожні content_type в anime_library"""
    conn = db()
    
    # Отримуємо всі записи без content_type
    rows = conn.execute(
        """SELECT slug FROM anime_library 
           WHERE content_type IS NULL OR content_type = ''"""
    ).fetchall()
    
    total = len(rows)
    
    if total == 0:
        print("✅ Всі записи вже мають content_type!")
        return
    
    print(f"📊 Знайдено {total} записів без content_type")
    print("🚀 Починаю заповнення...\n")
    
    updated = 0
    failed = 0
    
    async with aiohttp.ClientSession() as session:
        for i, (slug,) in enumerate(rows, 1):
            data = await fetch_anime_info(session, slug)
            
            if data and data.get("media_type"):
                content_type = data["media_type"]
                
                with transaction():
                    conn.execute(
                        "UPDATE anime_library SET content_type = ? WHERE slug = ?",
                        (content_type, slug)
                    )
                
                print(f"[{i}/{total}] ✅ {slug} -> {content_type}")
                updated += 1
            else:
                failed += 1
            
            # Затримка щоб не перевантажувати API
            await asyncio.sleep(0.3)
            
            # Прогрес кожні 50 записів
            if i % 50 == 0:
                print(f"\n📈 Прогрес: {i}/{total} | Оновлено: {updated} | Помилок: {failed}\n")
    
    print(f"\n{'='*50}")
    print(f"✅ ГОТОВО!")
    print(f"   Оновлено: {updated}")
    print(f"   Помилок: {failed}")
    print(f"   Всього: {total}")


if __name__ == "__main__":
    print("="*50)
    print("  Fill Content Types Script")
    print("="*50 + "\n")
    
    start = time.time()
    asyncio.run(fill_content_types())
    
    elapsed = time.time() - start
    print(f"\n⏱️ Час виконання: {elapsed:.1f} секунд")
