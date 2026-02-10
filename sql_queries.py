# =========================
# user_seen
# =========================
INSERT_USER_SEEN = """
    INSERT INTO user_seen(user_id, anime_id, seen_at) 
    VALUES(%s, %s, %s)
    ON CONFLICT (user_id, anime_id) DO UPDATE SET seen_at = EXCLUDED.seen_at
"""

SELECT_USER_SEEN_BY_USER = """
    SELECT anime_id FROM user_seen 
    WHERE user_id=%s 
    ORDER BY seen_at DESC 
    LIMIT %s
"""

DELETE_USER_SEEN_BY_USER = """
    DELETE FROM user_seen 
    WHERE user_id=%s
"""

# =========================
# user_feedback
# =========================
INSERT_USER_FEEDBACK = """
    INSERT INTO user_feedback(
        user_id, anime_id, value, ts,
        title, poster_url, hikka_url,
        year, score, episodes_total,
        genres_json, description,
        watch_links_json
    )
    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (user_id, anime_id) DO UPDATE SET
        value = EXCLUDED.value,
        ts = EXCLUDED.ts,
        title = EXCLUDED.title,
        poster_url = EXCLUDED.poster_url,
        hikka_url = EXCLUDED.hikka_url,
        year = EXCLUDED.year,
        score = EXCLUDED.score,
        episodes_total = EXCLUDED.episodes_total,
        genres_json = EXCLUDED.genres_json,
        description = EXCLUDED.description,
        watch_links_json = EXCLUDED.watch_links_json
"""

SELECT_USER_FEEDBACK_LIKES = """
    SELECT COUNT(*) FROM user_feedback 
    WHERE user_id=%s AND value=1
"""

SELECT_USER_FEEDBACK_LIKES_WITH_OFFSET = """
    SELECT anime_id, COALESCE(title, anime_id) AS title
    FROM user_feedback
    WHERE user_id=%s AND value=1
    ORDER BY ts DESC
    LIMIT 1 OFFSET %s
"""

SELECT_USER_FEEDBACK_SNAPSHOT = """
    SELECT anime_id, title, poster_url, hikka_url,
           year, score, episodes_total, genres_json,
           description, watch_links_json
    FROM user_feedback
    WHERE user_id=%s AND value=1
    ORDER BY ts DESC
    LIMIT 1 OFFSET %s
"""

DELETE_USER_FEEDBACK_LIKES = """
    DELETE FROM user_feedback 
    WHERE user_id=%s AND value=1
"""

DELETE_USER_FEEDBACK_BY_USER_ANIME = """
    DELETE FROM user_feedback 
    WHERE user_id=%s AND anime_id=%s AND value=1
"""

# =========================
# user_state
# =========================
SELECT_USER_STATE_LAST_PAGE = """
    SELECT last_page FROM user_state 
    WHERE user_id=%s
"""

INSERT_USER_STATE_LAST_PAGE = """
    INSERT INTO user_state(user_id, last_page, updated_at)
    VALUES(%s, %s, %s)
    ON CONFLICT(user_id) DO UPDATE SET
        last_page=EXCLUDED.last_page,
        updated_at=EXCLUDED.updated_at
"""

UPDATE_USER_STATE_CLEAR_PAGE = """
    UPDATE user_state 
    SET last_page=NULL, updated_at=%s 
    WHERE user_id=%s
"""

SELECT_USER_STATE_GENRES = """
    SELECT selected_genres_json FROM user_state 
    WHERE user_id = %s
"""

SELECT_USER_STATE_EXCLUDED_GENRES = """
    SELECT excluded_genres_json FROM user_state 
    WHERE user_id = %s
"""

INSERT_USER_STATE_GENRES = """
    INSERT INTO user_state (user_id, selected_genres_json, updated_at)
    VALUES (%s, %s, %s)
    ON CONFLICT(user_id) DO UPDATE SET
        selected_genres_json = EXCLUDED.selected_genres_json,
        updated_at = EXCLUDED.updated_at
"""

INSERT_USER_STATE_EXCLUDED_GENRES = """
    INSERT INTO user_state (user_id, excluded_genres_json, updated_at)
    VALUES (%s, %s, %s)
    ON CONFLICT(user_id) DO UPDATE SET
        excluded_genres_json = EXCLUDED.excluded_genres_json,
        updated_at = EXCLUDED.updated_at
"""

# =========================
# cb_map
# =========================
INSERT_CB_MAP = """
    INSERT INTO cb_map(
        cb_id, anime_id,
        description,
        watch_links_json, created_at
    )
    VALUES(%s, %s, %s, %s, %s)
    ON CONFLICT(anime_id) DO UPDATE SET
        description = EXCLUDED.description,
        watch_links_json = EXCLUDED.watch_links_json,
        created_at = EXCLUDED.created_at
"""

SELECT_CB_MAP_BY_ID = """
    SELECT m.anime_id, l.title, m.watch_links_json 
    FROM cb_map m
    LEFT JOIN anime_library l ON m.anime_id = l.slug
    WHERE m.cb_id=%s
"""

SELECT_CB_MAP_FULL = """
    SELECT m.anime_id, l.title, l.poster_url, l.hikka_url,
           l.year, l.score, l.episodes_total, l.genres_json,
           m.description, m.watch_links_json, l.ua_poster_url, l.content_type
    FROM cb_map m
    LEFT JOIN anime_library l ON m.anime_id = l.slug
    WHERE m.cb_id=%s
"""

SELECT_CB_MAP_BY_ANIME_ID = """
    SELECT l.title, m.cb_id 
    FROM cb_map m
    LEFT JOIN anime_library l ON m.anime_id = l.slug
    WHERE m.anime_id = %s 
    ORDER BY m.created_at DESC 
    LIMIT 1
"""

# =========================
# bot_meta
# =========================
SELECT_BOT_META = """
    SELECT value FROM bot_meta 
    WHERE key=%s
"""

INSERT_BOT_META = """
    INSERT INTO bot_meta(key, value, updated_at)
    VALUES(%s, %s, %s)
    ON CONFLICT(key) DO UPDATE SET
        value=EXCLUDED.value,
        updated_at=EXCLUDED.updated_at
"""

# =========================
# bot_users
# =========================
INSERT_BOT_USER = """
    INSERT INTO bot_users(user_id, first_seen_at, last_seen_at, username, first_name)
    VALUES(%s, %s, %s, %s, %s)
    ON CONFLICT(user_id) DO UPDATE SET
        last_seen_at=EXCLUDED.last_seen_at,
        username=COALESCE(EXCLUDED.username, bot_users.username),
        first_name=COALESCE(EXCLUDED.first_name, bot_users.first_name)
"""

SELECT_BOT_USERS_COUNT = """
    SELECT COUNT(*) FROM bot_users
"""


SELECT_BOT_USERS_ACTIVE = """
    SELECT COUNT(*) FROM bot_users 
    WHERE last_seen_at >= %s
"""

# =========================
# anime_library (Global Index)
# =========================
INSERT_LIBRARY_ITEM = """
    INSERT INTO anime_library(
        slug, title, genres_json, score, year, 
        episodes_total, poster_url, hikka_url, updated_at, ua_poster_url, content_type
    )
    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT(slug) DO UPDATE SET
        title=EXCLUDED.title,
        genres_json=CASE 
            WHEN anime_library.genres_json IS NOT NULL 
                 AND anime_library.genres_json != '[]' 
                 AND anime_library.genres_json != '' 
            THEN anime_library.genres_json 
            ELSE EXCLUDED.genres_json 
        END,
        score=EXCLUDED.score,
        year=EXCLUDED.year,
        episodes_total=EXCLUDED.episodes_total,
        poster_url=EXCLUDED.poster_url,
        hikka_url=EXCLUDED.hikka_url,
        updated_at=EXCLUDED.updated_at,
        ua_poster_url=EXCLUDED.ua_poster_url,
        content_type=EXCLUDED.content_type
"""

INSERT_LIBRARY_ITEM_SKIP_POSTER_UPDATE = """
    INSERT INTO anime_library(
        slug, title, genres_json, score, year, 
        episodes_total, poster_url, hikka_url, updated_at, ua_poster_url, content_type
    )
    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT(slug) DO UPDATE SET
        title=EXCLUDED.title,
        genres_json=CASE 
            WHEN anime_library.genres_json IS NOT NULL 
                 AND anime_library.genres_json != '[]' 
                 AND anime_library.genres_json != '' 
            THEN anime_library.genres_json 
            ELSE EXCLUDED.genres_json 
        END,
        score=EXCLUDED.score,
        year=EXCLUDED.year,
        episodes_total=EXCLUDED.episodes_total,
        poster_url=EXCLUDED.poster_url,
        hikka_url=EXCLUDED.hikka_url,
        updated_at=EXCLUDED.updated_at,
        ua_poster_url=COALESCE(anime_library.ua_poster_url, EXCLUDED.ua_poster_url),
        content_type=EXCLUDED.content_type
"""

COUNT_LIBRARY = """
    SELECT COUNT(*) FROM anime_library
"""

UPDATE_LIBRARY_GENRES = """
    UPDATE anime_library 
    SET genres_json = %s, updated_at = %s
    WHERE slug = %s
"""

