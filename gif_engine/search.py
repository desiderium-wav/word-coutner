import random
import logging
import time
from .db import connect
from .vectors import embed, similarity
from .config import (
    MAX_RESULTS, SIMILARITY_THRESHOLD, PREFER_EXTERNAL, EXTERNAL_API_ORDER, CACHE_TTL_DAYS
)
from .external_api import (
    search_e621, search_nekos, search_giphy, search_tenor
)

logger = logging.getLogger(__name__)

_async_external_map = {
    "e621": search_e621,
    "nekos": search_nekos,
    "giphy": search_giphy,
    "tenor": search_tenor
}

def _normalize_query(q: str) -> str:
    return (q or "").strip().lower()

async def _try_external_apis(query: str, allow_nsfw: bool):
    # check external cache first
    nq = _normalize_query(query)
    cache_ttl = int(time.time()) - CACHE_TTL_DAYS * 86400
    with connect() as db:
        c = db.cursor()
        c.execute(
            "SELECT source, url, nsfw, fetched_at FROM external_cache WHERE query=? ORDER BY fetched_at DESC",
            (nq,)
        )
        rows = c.fetchall()
        # filter out expired and nsfw mismatches
        valid = []
        for source, url, nsfw, fetched_at in rows:
            if fetched_at < cache_ttl:
                continue
            if nsfw and not allow_nsfw:
                continue
            valid.append({"url": url, "source": source, "nsfw": bool(nsfw)})
        if valid:
            chosen = random.choice(valid)
            logger.info("Returning cached external result for query=%s source=%s", query, chosen.get("source"))
            return chosen

    # No valid cached result -> try external APIs in configured order
    for name in EXTERNAL_API_ORDER:
        name = name.strip().lower()
        func = _async_external_map.get(name)
        if not func:
            continue
        try:
            res = await func(query, allow_nsfw=allow_nsfw)
            if res:
                # store in cache
                try:
                    with connect() as db:
                        c = db.cursor()
                        c.execute(
                            "INSERT INTO external_cache (source, query, url, nsfw, fetched_at) VALUES (?, ?, ?, ?, ?)",
                            (res.get("source"), nq, res.get("url"), int(res.get("nsfw") and 1 or 0), int(time.time()))
                        )
                        db.commit()
                except Exception as e:
                    logger.debug("Failed to write external_cache: %s", e)
                return res
        except Exception as e:
            logger.exception("External provider %s raised exception: %s", name, e)
            continue

    return None

async def search_gifs(query: str, allow_nsfw: bool):
    nq = _normalize_query(query)
    q_emb = None
    try:
        q_emb = embed(query)
    except Exception as e:
        logger.debug("Embedding failed for query '%s': %s", query, e)

    # Decide order based on PREFER_EXTERNAL
    if PREFER_EXTERNAL:
        # Try external APIs first
        res = await _try_external_apis(query, allow_nsfw=allow_nsfw)
        if res:
            return res
        # then local DB
    # Local DB search
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

            candidates = []
            for _, tid, nsfw_flag in scored[:MAX_RESULTS]:
                c.execute(
                    "SELECT url, source FROM media WHERE topic_id=? AND dead=0",
                    (tid,)
                )
                for url, source in c.fetchall():
                    if url:
                        candidates.append({"url": url, "source": source, "nsfw": nsfw_flag})

            if candidates:
                chosen = random.choice(candidates)
                logger.info("Local DB returned a match for query=%s url=%s", query, chosen.get("url"))
                return chosen

    # If not found locally and PREFER_EXTERNAL is False, try external now
    if not PREFER_EXTERNAL:
        res = await _try_external_apis(query, allow_nsfw=allow_nsfw)
        if res:
            return res

    # Nothing found
    logger.info("No results found for query=%s", query)
    return None
