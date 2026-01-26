import os
import sqlite3
from .config import DB_PATH

def _ensure_db_dir():
    # Ensure directory exists for DB_PATH (if path contains directories)
    dirpath = os.path.dirname(DB_PATH)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)

def connect():
    _ensure_db_dir()
    return sqlite3.connect(DB_PATH)

def init_db():
    """
    Initialize schema for gif database. This will attempt to create tables if
    missing and perform a safe migration to add the `source` column to media
    if an older DB exists.
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

        # Create media table if missing. We'll create with source column.
        c.execute("""
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            url TEXT UNIQUE,
            source TEXT,
            last_verified INTEGER,
            dead INTEGER DEFAULT 0
        )""")

        # Ensure queries table exists
        c.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            query TEXT
        )""")

        # Add indexes for faster lookups
        c.execute("CREATE INDEX IF NOT EXISTS idx_media_topic ON media(topic_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_media_last_verified ON media(last_verified)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_queries_topic ON queries(topic_id)")

        db.commit()

def init_gif_db():
    """
    Compatibility wrapper expected by main.py.
    """
    init_db()
