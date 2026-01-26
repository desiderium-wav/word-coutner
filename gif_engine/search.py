import random
from .db import connect
from .vectors import embed, similarity
from .config import MAX_RESULTS, SIMILARITY_THRESHOLD

def search(query: str, allow_nsfw: bool):
    q_emb = embed(query)

    with connect() as db:
        c = db.cursor()
        c.execute("SELECT id, nsfw, embedding FROM topics")
        topics = c.fetchall()

        scored = []
        for tid, nsfw, emb in topics:
            if nsfw and not allow_nsfw:
                continue
            sim = similarity(emb, q_emb)
            if sim >= SIMILARITY_THRESHOLD:
                scored.append((sim, tid))

        scored.sort(reverse=True)
        results = []

        for _, tid in scored[:MAX_RESULTS]:
            c.execute(
                "SELECT url FROM media WHERE topic_id=? AND dead=0",
                (tid,)
            )
            urls = [r[0] for r in c.fetchall()]
            results.extend(urls)

    return random.choice(results) if results else None
