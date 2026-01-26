import random
import logging
from .db import connect
from .vectors import embed, similarity
from .config import MAX_RESULTS, SIMILARITY_THRESHOLD
from .external_api import search_e621, search_nekos

logger = logging.getLogger(__name__)

async def search_gifs(query: str, allow_nsfw: bool):
    """
    Async function that searches the local DB first, then falls back to external APIs.
    Returns a dict {url, source, nsfw} or None.
    """
    try:
        q_emb = embed(query)
    except Exception as e:
        logger.exception("Embedding failed for query '%s': %s", query, e)
        q_emb = None

    candidates = []
    if q_emb is not None:
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
                    logger.debug("Skipping topic %s during similarity computation", tid)
                    continue
                if sim >= SIMILARITY_THRESHOLD:
                    scored.append((sim, tid, bool(nsfw)))

            scored.sort(reverse=True, key=lambda x: x[0])

            for _, tid, nsfw_flag in scored[:MAX_RESULTS]:
                c.execute(
                    "SELECT url, source FROM media WHERE topic_id=? AND dead=0",
                    (tid,)
                )
                for url, source in c.fetchall():
                    if url:
                        candidates.append({"url": url, "source": source, "nsfw": nsfw_flag})

    # If we have local candidates, return a random one
    if candidates:
        chosen = random.choice(candidates)
        logger.info("Local search matched query=%s url=%s", query, chosen.get("url"))
        return chosen

    # Local DB had nothing; try external sources in order of preference
    logger.info("No local match for '%s'; falling back to external APIs", query)

    # Try e621 if enabled and allowed by config
    try:
        res = await search_e621(query, allow_nsfw=allow_nsfw)
        if res:
            logger.info("e621 provided result for query=%s", query)
            return res
    except Exception as e:
        logger.exception("e621 search raised: %s", e)

    # Try nekos.life
    try:
        res = await search_nekos(query, allow_nsfw=allow_nsfw)
        if res:
            logger.info("nekos provided result for query=%s", query)
            return res
    except Exception as e:
        logger.exception("nekos search raised: %s", e)

    # Nothing found
    logger.info("No external results for '%s'", query)
    return None
