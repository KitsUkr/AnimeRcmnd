"""
Скрипт міграції даних з SQLite на PostgreSQL.
Запустіть один раз: python migrate_sqlite_to_postgres.py
"""
import sqlite3
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = "bot.sqlite3"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError("Вкажи DATABASE_URL у .env")


def create_tables(pg_conn):
    """Створення всіх таблиць у PostgreSQL"""
    cur = pg_conn.cursor()
    
    # user_seen
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_seen (
            user_id BIGINT NOT NULL,
            anime_id TEXT NOT NULL,
            seen_at BIGINT NOT NULL,
            PRIMARY KEY (user_id, anime_id)
        )
    """)
    
    # user_feedback
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_feedback (
            user_id BIGINT NOT NULL,
            anime_id TEXT NOT NULL,
            value INTEGER NOT NULL,
            ts BIGINT NOT NULL,
            title TEXT,
            poster_url TEXT,
            hikka_url TEXT,
            year INTEGER,
            score REAL,
            episodes_total INTEGER,
            genres_json TEXT,
            description TEXT,
            watch_links_json TEXT,
            PRIMARY KEY (user_id, anime_id)
        )
    """)
    
    # user_state
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id BIGINT PRIMARY KEY,
            last_page INTEGER DEFAULT NULL,
            updated_at BIGINT NOT NULL,
            selected_genres_json TEXT DEFAULT '[]',
            excluded_genres_json TEXT DEFAULT '[]',
            selected_content_types_json TEXT DEFAULT '[]',
            excluded_content_types_json TEXT DEFAULT '[]',
            genre_snapshot_json TEXT DEFAULT '[]',
            filter_alert_shown INTEGER DEFAULT 0,
            in_genre_menu INTEGER DEFAULT 0,
            genre_menu_message_id BIGINT DEFAULT NULL,
            genre_hint_shown INTEGER DEFAULT 0,
            year_from INTEGER DEFAULT NULL,
            year_to INTEGER DEFAULT NULL,
            last_recommendation_at BIGINT DEFAULT NULL
        )
    """)
    
    # bot_users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id BIGINT PRIMARY KEY,
            first_seen_at BIGINT NOT NULL,
            last_seen_at BIGINT NOT NULL,
            username TEXT,
            first_name TEXT
        )
    """)
    
    # bot_meta
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at BIGINT NOT NULL
        )
    """)
    
    # cb_map
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cb_map (
            cb_id TEXT PRIMARY KEY,
            anime_id TEXT NOT NULL,
            description TEXT,
            watch_links_json TEXT NOT NULL DEFAULT '[]',
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cb_map_anime_unique ON cb_map(anime_id)")
    
    # anime_library
    cur.execute("""
        CREATE TABLE IF NOT EXISTS anime_library (
            slug TEXT PRIMARY KEY,
            title TEXT,
            genres_json TEXT NOT NULL DEFAULT '[]',
            score REAL,
            year INTEGER,
            episodes_total INTEGER,
            poster_url TEXT,
            hikka_url TEXT,
            ua_poster_url TEXT,
            updated_at BIGINT NOT NULL,
            content_type TEXT
        )
    """)
    
    # anime_details_cache
    cur.execute("""
        CREATE TABLE IF NOT EXISTS anime_details_cache (
            slug TEXT PRIMARY KEY,
            description TEXT,
            watch_links_json TEXT NOT NULL DEFAULT '[]',
            updated_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL
        )
    """)
    
    # Індекси
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_seen_user_seen_at 
        ON user_seen(user_id, seen_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_feedback_user_value_ts 
        ON user_feedback(user_id, value, ts DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_cb_map_anime_created 
        ON cb_map(anime_id, created_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bot_users_last_seen 
        ON bot_users(last_seen_at)
    """)
    
    pg_conn.commit()
    print("[OK] Tablitsy stvoreno uspisno")


def migrate_table(sqlite_conn, pg_conn, table_name, columns, insert_query):
    """Мігрувати дані з однієї таблиці"""
    sqlite_cur = sqlite_conn.cursor()
    pg_cur = pg_conn.cursor()
    
    # Перевіряємо чи існує таблиця в SQLite
    sqlite_cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
    if not sqlite_cur.fetchone():
        print(f"[SKIP] Tabla {table_name} ne isnue v SQLite, propuskayu")
        return 0
    
    # Отримуємо дані з SQLite
    col_list = ", ".join(columns)
    sqlite_cur.execute(f"SELECT {col_list} FROM {table_name}")
    rows = sqlite_cur.fetchall()
    
    if not rows:
        print(f"[SKIP] Tabla {table_name} porojnya, propuskayu")
        return 0
    
    # Вставляємо в PostgreSQL
    count = 0
    for row in rows:
        try:
            pg_cur.execute(insert_query, row)
            count += 1
        except Exception as e:
            # Якщо конфлікт - пропускаємо (дані вже є)
            pg_conn.rollback()
            continue
    
    pg_conn.commit()
    print(f"[OK] {table_name}: migrovano {count}/{len(rows)} zapisiv")
    return count


def main():
    print("[START] Pochatok migraciyi SQLite -> PostgreSQL")
    print(f"   SQLite: {SQLITE_PATH}")
    print(f"   PostgreSQL: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    print()
    
    # Підключаємось
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    pg_conn = psycopg2.connect(DATABASE_URL)
    
    try:
        # Створюємо таблиці
        create_tables(pg_conn)
        print()
        
        # Мігруємо дані
        total = 0
        
        # user_seen
        total += migrate_table(
            sqlite_conn, pg_conn, "user_seen",
            ["user_id", "anime_id", "seen_at"],
            "INSERT INTO user_seen(user_id, anime_id, seen_at) VALUES(%s, %s, %s) ON CONFLICT DO NOTHING"
        )
        
        # user_feedback
        total += migrate_table(
            sqlite_conn, pg_conn, "user_feedback",
            ["user_id", "anime_id", "value", "ts", "title", "poster_url", "hikka_url", 
             "year", "score", "episodes_total", "genres_json", "description", "watch_links_json"],
            """INSERT INTO user_feedback(user_id, anime_id, value, ts, title, poster_url, hikka_url, 
               year, score, episodes_total, genres_json, description, watch_links_json) 
               VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"""
        )
        
        # user_state
        total += migrate_table(
            sqlite_conn, pg_conn, "user_state",
            ["user_id", "last_page", "updated_at", "selected_genres_json", "excluded_genres_json",
             "selected_content_types_json", "excluded_content_types_json", "genre_snapshot_json",
             "filter_alert_shown", "in_genre_menu", "genre_menu_message_id", "genre_hint_shown",
             "year_from", "year_to", "last_recommendation_at"],
            """INSERT INTO user_state(user_id, last_page, updated_at, selected_genres_json, excluded_genres_json,
               selected_content_types_json, excluded_content_types_json, genre_snapshot_json,
               filter_alert_shown, in_genre_menu, genre_menu_message_id, genre_hint_shown,
               year_from, year_to, last_recommendation_at) 
               VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"""
        )
        
        # bot_users
        total += migrate_table(
            sqlite_conn, pg_conn, "bot_users",
            ["user_id", "first_seen_at", "last_seen_at", "username", "first_name"],
            "INSERT INTO bot_users(user_id, first_seen_at, last_seen_at, username, first_name) VALUES(%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
        )
        
        # bot_meta
        total += migrate_table(
            sqlite_conn, pg_conn, "bot_meta",
            ["key", "value", "updated_at"],
            "INSERT INTO bot_meta(key, value, updated_at) VALUES(%s, %s, %s) ON CONFLICT DO NOTHING"
        )
        
        # cb_map
        total += migrate_table(
            sqlite_conn, pg_conn, "cb_map",
            ["cb_id", "anime_id", "description", "watch_links_json", "created_at"],
            "INSERT INTO cb_map(cb_id, anime_id, description, watch_links_json, created_at) VALUES(%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
        )
        
        # anime_library
        total += migrate_table(
            sqlite_conn, pg_conn, "anime_library",
            ["slug", "title", "genres_json", "score", "year", "episodes_total", 
             "poster_url", "hikka_url", "ua_poster_url", "updated_at", "content_type"],
            """INSERT INTO anime_library(slug, title, genres_json, score, year, episodes_total, 
               poster_url, hikka_url, ua_poster_url, updated_at, content_type) 
               VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"""
        )
        
        # anime_details_cache
        total += migrate_table(
            sqlite_conn, pg_conn, "anime_details_cache",
            ["slug", "description", "watch_links_json", "updated_at", "expires_at"],
            "INSERT INTO anime_details_cache(slug, description, watch_links_json, updated_at, expires_at) VALUES(%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
        )
        
        print()
        print(f"[DONE] Migraciyu zaversheno! Vsyogo migrovano: {total} zapisiv")
        
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
