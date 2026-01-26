import time
from .db import connect
from .vectors import embed, similarity
from .config import SIMILARITY_THRESHOLD

def ingest_result(query: str, url: str, source: str, nsfw: bool):
    """
    Compatibility function expected by main.py.

    - Synchronous (main calls this without awaiting).
    - Stores `source` on the media row (adds column in init_db migration).
    """
    emb = embed(query)

    with connect() as db:
        c = db.cursor()

        # load existing topics (id, embedding)
        c.execute("SELECT id, embedding FROM topics")
        rows = c.fetchall()

        topic_id = None
        for tid, existing in rows:
            try:
                if similarity(existing, emb) >= SIMILARITY_THRESHOLD:
                    topic_id = tid
                    break
            except Exception:
                # If any embedding is corrupt or incompatible, skip it
                continue

        if topic_id is None:
            c.execute(
                "INSERT INTO topics (canonical, nsfw, embedding) VALUES (?, ?, ?)",
                (query.lower(), int(nsfw), emb)
            )
            topic_id = c.lastrowid
        else:
            c.execute(
                "UPDATE topics SET nsfw = nsfw OR ? WHERE id = ?",
                (int(nsfw), topic_id)
            )

        # store media with source and timestamp; ignore duplicates by url
        c.execute(
            "INSERT OR IGNORE INTO media (topic_id, url, source, last_verified, dead) VALUES (?, ?, ?, ?, 0)",
            (topic_id, url, source, int(time.time()))
        )

        # store original query for auditing / later re-indexing
        c.execute(
            "INSERT INTO queries (topic_id, query) VALUES (?, ?)",
            (topic_id, query)
        )

        db.commit()
