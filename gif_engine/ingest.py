import time
from .db import connect
from .vectors import embed, similarity
from .config import SIMILARITY_THRESHOLD

def ingest(query: str, url: str, nsfw: bool):
    emb = embed(query)

    with connect() as db:
        c = db.cursor()

        c.execute("SELECT id, embedding FROM topics")
        rows = c.fetchall()

        topic_id = None
        for tid, existing in rows:
            if similarity(existing, emb) >= SIMILARITY_THRESHOLD:
                topic_id = tid
                break

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

        c.execute(
            "INSERT OR IGNORE INTO media (topic_id, url, last_verified, dead) VALUES (?, ?, ?, 0)",
            (topic_id, url, int(time.time()))
        )

        c.execute(
            "INSERT INTO queries (topic_id, query) VALUES (?, ?)",
            (topic_id, query)
        )

        db.commit()
