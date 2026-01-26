import os
import sqlite3
import logging
from .config import DB_PATH

logger = logging.getLogger(__name__)

def _ensure_db_dir():
    dirpath = os.path.dirname(DB_PATH)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
        logger.info("Created database directory %s", dirpath)

def _apply_pragmas(conn: sqlite3.Connection):
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    except Exception as e:
        logger.warning("Failed to set PRAGMA on DB: %s", e)

def connect():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn

def init_db():
    """
    Initialize schema for gif database and perform safe migrations:
    - Ensure media table has columns: source, content_type, width, height
    - Ensure external_cache table exists
    - Create helpful indexes
    """
    _ensure_db_dir()
    with connect() as db:
        c = db.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY,
            canonical TEXT,
            nsfw INTEGER,
            embedding BLOB
        )""")

        # Create media table with the expanded schema
        c.execute("""
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            url TEXT UNIQUE,
            source TEXT,
            content_type TEXT,
            width INTEGER,
            height INTEGER,
            last_verified INTEGER,
            dead INTEGER DEFAULT 0
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            query TEXT
        )""")

        # Create external cache table for external API results
        c.execute("""
        CREATE TABLE IF NOT EXISTS external_cache (
            id INTEGER PRIMARY KEY,
            source TEXT,
            query TEXT,
            url TEXT,
            nsfw INTEGER,
            fetched_at INTEGER
        )""")

        # Indexes for performance
        c.execute("CREATE INDEX IF NOT EXISTS idx_media_topic ON media(topic_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_media_last_verified ON media(last_verified)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_queries_topic ON queries(topic_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_external_cache_query ON external_cache(query)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_external_cache_source ON external_cache(source)")

        # Safe migrations for older DBs: add columns to media if missing
        try:
            existing_cols = [r[1] for r in c.execute("PRAGMA table_info(media)").fetchall()]
            needed = [
                ("source", "TEXT"),
                ("content_type", "TEXT"),
                ("width", "INTEGER"),
                ("height", "INTEGER")
            ]
            for col, coltype in needed:
                if col not in existing_cols:
                    sql = f"ALTER TABLE media ADD COLUMN {col} {coltype}"
                    logger.info("Applying migration: %s", sql)
                    c.execute(sql)
        except Exception as e:
            logger.exception("Migration step failed: %s", e)

        db.commit()

def init_gif_db():
    init_db()
