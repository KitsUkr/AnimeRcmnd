"""
Скрипт для перевірки та міграції при переході на консолідовану синхронізацію жанрів.

Запуск:
    python допоміжні/migrate_to_consolidated_sync.py

Цей скрипт:
1. Перевіряє наявність жанрів у bot_meta (available_genres_json)
2. Якщо жанри відсутні, пропонує завантажити їх при першій синхронізації
3. Логує інформацію про стан міграції
"""

import sys
import json
import sqlite3
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from hikka_client import META_AVAILABLE_GENRES
from database import db, transaction, meta_get, meta_set


def check_migration_status() -> dict:
    """Перевіряє статус міграції жанрів"""
    status = {
        "genres_in_meta": False,
        "genres_count": 0,
        "status": "NOT_MIGRATED",
        "message": ""
    }
    
    # Перевіряємо наявність жанрів у bot_meta
    cached_genres = meta_get(META_AVAILABLE_GENRES)
    
    if not cached_genres:
        status["status"] = "NOT_MIGRATED"
        status["message"] = "⚠️ Жанри не знайдені в bot_meta. Будуть завантажені при першій синхронізації."
        return status
    
    # Парсимо жанри
    try:
        genres_data = json.loads(cached_genres[0])
        if isinstance(genres_data, list):
            status["genres_count"] = len(genres_data)
            status["genres_in_meta"] = True
        else:
            status["status"] = "ERROR"
            status["message"] = "❌ Невірний формат жанрів у bot_meta"
            return status
    except Exception as e:
        status["status"] = "ERROR"
        status["message"] = f"❌ Помилка парсингу жанрів: {e}"
        return status
    
    # Жанри знайдені - все добре
    status["status"] = "OK"
    status["message"] = f"✅ Жанри успішно мігровані ({status['genres_count']} записів)"
    return status


def print_migration_status(status: dict) -> None:
    """Виводить статус міграції"""
    print("\n" + "="*60)
    print("📊 СТАТУС МІГРАЦІЇ ЖАНРІВ")
    print("="*60)
    print(f"Статус: {status['status']}")
    print(f"Повідомлення: {status['message']}")
    
    if status['genres_in_meta']:
        print(f"Жанрів у базі: {status['genres_count']}")
    
    print("="*60 + "\n")


def verify_schema() -> bool:
    """Перевіряє, що таблиця bot_meta існує"""
    try:
        conn = db()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_meta'"
        )
        result = cursor.fetchone()
        return result is not None
    except Exception as e:
        print(f"❌ Помилка перевірки таблиці: {e}")
        return False


def main():
    """Головна функція міграції"""
    print("\n🔄 МІГРАЦІЯ ЖАНРІВ НА КОНСОЛІДОВАНУ СИНХРОНІЗАЦІЮ")
    print("="*60)
    
    # 1. Перевіряємо наявність БД
    if not Path("bot.sqlite3").exists():
        print("⚠️ База даних bot.sqlite3 не знайдена")
        print("Вона буде створена при першому запуску бота")
        return
    
    # 2. Перевіряємо таблицю bot_meta
    if not verify_schema():
        print("❌ Таблиця bot_meta не знайдена в базі")
        print("Переконайтесь, що база ініціалізована коректно")
        return
    
    # 3. Перевіряємо статус міграції
    status = check_migration_status()
    print_migration_status(status)
    
    # 4. Даємо рекомендації
    print("📋 РЕКОМЕНДАЦІЇ:")
    print("-"*60)
    
    if status["status"] == "NOT_MIGRATED":
        print("1. ✅ Жанри будуть завантажені автоматично при запуску /force_sync")
        print("2. ✅ Синхронізація відбудеться у фоні при запуску бота")
        print("3. ✅ Жодних додаткових дій не потрібно")
        print("\nДалі:")
        print("  • Запустіть бота: python UaAnimeRcmd.py")
        print("  • Введіть /force_sync для негайної синхронізації")
        print("  • Перевірте меню жанрів - вони повинні відображатися коректно")
    
    elif status["status"] == "OK":
        print("✅ Міграція завершена успішно!")
        print("✅ Жанри готові до використання")
        print("✅ Жодних дій не потрібно")
    
    elif status["status"] == "ERROR":
        print("❌ Помилка міграції:")
        print(f"  {status['message']}")
        print("\n💡 Рішення:")
        print("  • Запустіть /force_sync для перезавантаження жанрів")
        print("  • Якщо проблема повторюється, видаліть bot.sqlite3 та перезапустіть бота")
    
    print("\n" + "="*60 + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Критична помилка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
