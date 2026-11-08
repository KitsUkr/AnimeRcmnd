from database.connection import db, transaction


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS anime_library (
        slug TEXT PRIMARY KEY,
        title TEXT,
        genres_json TEXT,
        score REAL,
        year INTEGER,
        episodes_total INTEGER,
        poster_url TEXT,
        hikka_url TEXT,
        updated_at BIGINT,
        ua_poster_url TEXT,
        content_type TEXT,
        season TEXT,
        inserted_at BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_seen (
        user_id BIGINT NOT NULL,
        anime_id TEXT NOT NULL,
        seen_at BIGINT NOT NULL,
        PRIMARY KEY (user_id, anime_id)
    )
    """,
    """
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
        ua_poster_url TEXT,
        content_type TEXT,
        PRIMARY KEY (user_id, anime_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_state (
        user_id BIGINT PRIMARY KEY,
        last_page INTEGER,
        updated_at BIGINT,
        selected_genres_json TEXT,
        excluded_genres_json TEXT,
        selected_content_types_json TEXT,
        excluded_content_types_json TEXT,
        year_from INTEGER,
        year_to INTEGER,
        selected_seasons_json TEXT,
        excluded_seasons_json TEXT,
        rating_min REAL,
        filter_alert_shown INTEGER DEFAULT 0,
        last_recommendation_at BIGINT,
        in_genre_menu INTEGER DEFAULT 0,
        genre_menu_message_id BIGINT,
        genre_hint_shown INTEGER DEFAULT 0,
        genre_snapshot_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cb_map (
        cb_id TEXT PRIMARY KEY,
        anime_id TEXT NOT NULL UNIQUE,
        description TEXT,
        watch_links_json TEXT,
        created_at BIGINT,
        season TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anime_details_cache (
        slug TEXT PRIMARY KEY,
        description TEXT,
        watch_links_json TEXT,
        updated_at BIGINT NOT NULL,
        expires_at BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_users (
        user_id BIGINT PRIMARY KEY,
        first_seen_at BIGINT NOT NULL,
        last_seen_at BIGINT NOT NULL,
        username TEXT,
        first_name TEXT,
        ad_source TEXT,
        notifications_enabled BOOLEAN DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ad_campaigns (
        campaign_id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hikka_tokens (
        user_id BIGINT PRIMARY KEY,
        hikka_token TEXT NOT NULL,
        hikka_username TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
    """,
    "ALTER TABLE user_seen ALTER COLUMN user_id TYPE BIGINT",
    "ALTER TABLE user_feedback ALTER COLUMN user_id TYPE BIGINT",
    "ALTER TABLE user_state ALTER COLUMN user_id TYPE BIGINT",
    "ALTER TABLE bot_users ALTER COLUMN user_id TYPE BIGINT",
    "ALTER TABLE hikka_tokens ALTER COLUMN user_id TYPE BIGINT",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS selected_seasons_json TEXT",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS excluded_seasons_json TEXT",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS rating_min REAL",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS filter_alert_shown INTEGER DEFAULT 0",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS last_recommendation_at BIGINT",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS in_genre_menu INTEGER DEFAULT 0",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS genre_menu_message_id BIGINT",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS genre_hint_shown INTEGER DEFAULT 0",
    "ALTER TABLE user_state ADD COLUMN IF NOT EXISTS genre_snapshot_json TEXT",
    "ALTER TABLE anime_library ADD COLUMN IF NOT EXISTS season TEXT",
    "ALTER TABLE anime_library ADD COLUMN IF NOT EXISTS inserted_at BIGINT",
    "ALTER TABLE cb_map ADD COLUMN IF NOT EXISTS season TEXT",
    "ALTER TABLE cb_map ADD COLUMN IF NOT EXISTS watch_links_json TEXT",
    "ALTER TABLE user_feedback ADD COLUMN IF NOT EXISTS ua_poster_url TEXT",
    "ALTER TABLE user_feedback ADD COLUMN IF NOT EXISTS content_type TEXT",
    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS ad_source TEXT",
    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE",
    "CREATE INDEX IF NOT EXISTS idx_user_seen_user_seen_at ON user_seen(user_id, seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_user_feedback_user_value_ts ON user_feedback(user_id, value, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_anime_inserted_at ON anime_library(inserted_at DESC) WHERE inserted_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_anime_details_expires ON anime_details_cache(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_bot_users_last_seen ON bot_users(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_bot_users_notifications ON bot_users(notifications_enabled) INCLUDE (user_id)",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS idx_anime_library_title_trgm ON anime_library USING GIN (title gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_anime_library_genres ON anime_library USING GIN ((genres_json::jsonb))",
)


async def init_schema() -> None:
    async with transaction():
        conn = db()
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)
