import time
import logging
from .db import connect
from .vectors import embed, similarity
from .config import SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)

def ingest_result(query: str, url: str, source: str, nsfw: bool, content_type: str = None, width: int = None, height: int = None):
    """
    Synchronous compatibility function expected by main.py.

    - Stores `source` and optional metadata (content_type, width, height)
    - Resilient to corrupt existing embeddings
    """
    try:
        emb = embed(query)
    except Exception as e:
        logger.exception("Failed to create embedding for query '%s': %s", query, e)
        return

    with connect() as db:
        c = db.cursor()

        # load existing topics (id, embedding)
        c.execute("SELECT id, embedding FROM topics")
        rows = c.fetchall()

        topic_id = None
        for row in rows:
            tid = row[0]
            existing = row[1]
            try:
                if similarity(existing, emb) >= SIMILARITY_THRESHOLD:
                    topic_id = tid
                    break
            except Exception:
                # If embedding is corrupt or mismatched, skip it
                logger.debug("Skipping topic %s due to embedding error", tid)
                continue

        if topic_id is None:
            c.execute(
                "INSERT INTO topics (canonical, nsfw, embedding) VALUES (?, ?, ?)",
                (query.lower(), int(nsfw), emb)
            )
            topic_id = c.lastrowid
            logger.info("Created new topic id=%s for query=%s", topic_id, query)
        else:
            c.execute(
                "UPDATE topics SET nsfw = nsfw OR ? WHERE id = ?",
                (int(nsfw), topic_id)
            )
            logger.debug("Associated with existing topic id=%s", topic_id)

        # store media with source and timestamp; ignore duplicates by url
        try:
            c.execute(
                "INSERT OR IGNORE INTO media (topic_id, url, source, content_type, width, height, last_verified, dead) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (topic_id, url, source, content_type, width, height, int(time.time()))
            )
        except Exception as e:
            logger.exception("Failed to insert media row: %s", e)

        # store original query for auditing / later re-indexing
        try:
            c.execute(
                "INSERT INTO queries (topic_id, query) VALUES (?, ?)",
                (topic_id, query)
            )
        except Exception:
            # queries are non-critical
            pass

        db.commit()
