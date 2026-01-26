import random
from .db import connect
from .vectors import embed, similarity
from .config import MAX_RESULTS, SIMILARITY_THRESHOLD

async def search_gifs(query: str, allow_nsfw: bool):
    """
    Async compatibility function expected by main.py.

    Returns a dict with keys: url, source, nsfw (bool) or None if nothing found.
    Uses local semantic search first by comparing topic embeddings stored in DB.
    """
    q_emb = embed(query)

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
                continue
            if sim >= SIMILARITY_THRESHOLD:
                scored.append((sim, tid, bool(nsfw)))

        # sort by similarity descending
        scored.sort(reverse=True, key=lambda x: x[0])

        candidates = []
        # limit to top-N topics to avoid too many DB hits
        for _, tid, nsfw_flag in scored[:MAX_RESULTS]:
            c.execute(
                "SELECT url, source FROM media WHERE topic_id=? AND dead=0",
                (tid,)
            )
            for url, source in c.fetchall():
                if url:
                    candidates.append({"url": url, "source": source, "nsfw": nsfw_flag})

    return random.choice(candidates) if candidates else None
