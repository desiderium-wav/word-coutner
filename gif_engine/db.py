import sqlite3
from .config import DB_PATH

def connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    with connect() as db:
        c = db.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY,
            canonical TEXT,
            nsfw INTEGER,
            embedding BLOB
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            url TEXT UNIQUE,
            last_verified INTEGER,
            dead INTEGER DEFAULT 0
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            query TEXT
        )""")

        db.commit()
