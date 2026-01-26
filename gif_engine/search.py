import random
import logging
from .db import connect
from .vectors import embed, similarity
from .config import MAX_RESULTS, SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)

async def search_gifs(query: str, allow_nsfw: bool):
    """
    Async compatibility function expected by main.py.

    Returns a dict with keys: url, source, nsfw (bool) or None if nothing found.
    """
    try:
        q_emb = embed(query)
    except Exception as e:
        logger.exception("Embedding failure for query '%s': %s", query, e)
        return None

    candidates = []
    with connect() as db:
        c = db.cursor()
        c.execute("SELECT id, nsfw, embedding FROM topics")
        topics = c.fetchall()

        scored = []
        for tid, nsfw, emb in topics:
            if nsfw and not allow_nsfw:
                continue
            try:
                sim = similarity(emb, q_emb)
            except Exception:
                logger.debug("Skipping topic %s due to similarity error", tid)
                continue
            if sim >= SIMILARITY_THRESHOLD:
                scored.append((sim, tid, bool(nsfw)))

        # sort by similarity descending
        scored.sort(reverse=True, key=lambda x: x[0])

        # collect candidate media rows from top topics
        for _, tid, nsfw_flag in scored[:MAX_RESULTS]:
            c.execute(
                "SELECT url, source FROM media WHERE topic_id=? AND dead=0",
                (tid,)
            )
            for url, source in c.fetchall():
                if url:
                    candidates.append({"url": url, "source": source, "nsfw": nsfw_flag})

    if not candidates:
        logger.debug("No candidates found for query '%s'", query)
        return None

    chosen = random.choice(candidates)
    logger.info("search_gifs matched query=%s to url=%s (nsfw=%s)", query, chosen.get("url"), chosen.get("nsfw"))
    return chosen
