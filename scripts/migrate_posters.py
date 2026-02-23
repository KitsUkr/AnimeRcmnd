"""
Скрипт для перенесення українських постерів з posters.db до bot.sqlite3

Переносить тільки постери, де telegram_ok = 1
Зв'язок між таблицями: hikka_url в posters.db містить slug, який відповідає
slug в bot.sqlite3.anime_library
"""

import sqlite3
import re


def extract_slug_from_hikka_url(hikka_url: str) -> str | None:
    """Витягує slug з hikka_url, наприклад:
    https://hikka.io/anime/ginga-tetsudou-no-yoru-c10532 -> ginga-tetsudou-no-yoru-c10532
    """
    match = re.search(r'hikka\.io/anime/([a-zA-Z0-9\-]+)', hikka_url)
    if match:
        return match.group(1)
    return None


def migrate_posters():
    # Відкриваємо обидві бази
    posters_conn = sqlite3.connect('posters.db')
    posters_cursor = posters_conn.cursor()
    
    bot_conn = sqlite3.connect('bot.sqlite3')
    bot_cursor = bot_conn.cursor()
    
    # Отримуємо всі валідні постери
    posters_cursor.execute("""
        SELECT hikka_url, full_url 
        FROM posters 
        WHERE telegram_ok = 1
    """)
    valid_posters = posters_cursor.fetchall()
    
    print(f"Знайдено {len(valid_posters)} валідних постерів для міграції")
    
    # Створюємо словник slug -> poster_url
    # Якщо є кілька постерів для одного аніме, беремо перший
    slug_to_poster: dict[str, str] = {}
    for hikka_url, poster_url in valid_posters:
        slug = extract_slug_from_hikka_url(hikka_url)
        if slug and slug not in slug_to_poster:
            slug_to_poster[slug] = poster_url
    
    print(f"Унікальних аніме з постерами: {len(slug_to_poster)}")
    
    # Оновлюємо записи в bot.sqlite3
    updated_count = 0
    not_found_count = 0
    
    for slug, poster_url in slug_to_poster.items():
        # Перевіряємо чи існує такий slug в anime_library
        bot_cursor.execute("SELECT slug FROM anime_library WHERE slug = ?", (slug,))
        if bot_cursor.fetchone():
            bot_cursor.execute("""
                UPDATE anime_library 
                SET ua_poster_url = ? 
                WHERE slug = ?
            """, (poster_url, slug))
            updated_count += 1
        else:
            not_found_count += 1
            if not_found_count <= 10:  # Показуємо тільки перші 10
                print(f"  Не знайдено в anime_library: {slug}")
    
    bot_conn.commit()
    
    print(f"\n=== РЕЗУЛЬТАТ ===")
    print(f"Оновлено записів: {updated_count}")
    print(f"Не знайдено в anime_library: {not_found_count}")
    
    # Показати статистику
    bot_cursor.execute("SELECT COUNT(*) FROM anime_library WHERE ua_poster_url IS NOT NULL AND ua_poster_url != ''")
    total_with_ua_posters = bot_cursor.fetchone()[0]
    print(f"Всього записів з укр. постерами: {total_with_ua_posters}")
    
    posters_conn.close()
    bot_conn.close()


if __name__ == "__main__":
    migrate_posters()
