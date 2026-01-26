import aiohttp
import time
import asyncio
import logging
from .db import connect
from .config import VERIFY_INTERVAL_DAYS, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

# Tuning parameters (can be adjusted)
BATCH_SIZE = 20         # number of URLs to check per batch
BATCH_DELAY = 1.0       # seconds to sleep between batches to rate-limit

async def _prune():
    cutoff = time.time() - VERIFY_INTERVAL_DAYS * 86400

    with connect() as db:
        c = db.cursor()
        c.execute("""
            SELECT id, url FROM media
            WHERE dead=0 AND last_verified < ?
            ORDER BY last_verified ASC
        """, (int(cutoff),))
        rows = c.fetchall()

    if not rows:
        logger.info("prune: nothing to verify (no stale rows).")
        return

    logger.info("prune: verifying %d stale media rows", len(rows))

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # process in batches
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i+BATCH_SIZE]
            tasks = []
            for mid, url in batch:
                tasks.append(_check_and_update(session, mid, url))
            # run batch concurrently
            await asyncio.gather(*tasks)
            # rate limit between batches
            if i + BATCH_SIZE < len(rows):
                logger.debug("prune: sleeping %ss between batches", BATCH_DELAY)
                await asyncio.sleep(BATCH_DELAY)

async def _check_and_update(session: aiohttp.ClientSession, mid: int, url: str):
    ok = False
    try:
        # Try HEAD first
        async with session.head(url) as r:
            ok = r.status < 400
            logger.debug("prune HEAD %s -> %s", url, r.status)
    except Exception as e_head:
        logger.debug("prune HEAD failed for %s: %s; trying GET", url, e_head)
        try:
            async with session.get(url) as r:
                ok = r.status < 400
                logger.debug("prune GET %s -> %s", url, r.status)
        except Exception as e_get:
            logger.warning("prune GET also failed for %s: %s", url, e_get)
            ok = False

    try:
        with connect() as db:
            c = db.cursor()
            c.execute("""
                UPDATE media
                SET dead=?, last_verified=?
                WHERE id=?
            """, (0 if ok else 1, int(time.time()), mid))
            db.commit()
            logger.info("prune: updated id=%s url=%s alive=%s", mid, url, ok)
    except Exception as e:
        logger.exception("prune: DB update failed for id=%s url=%s: %s", mid, url, e)

def prune_dead_urls():
    """
    Compatibility wrapper expected by main.py which calls this synchronously
    from an async task. This schedules the pruning coroutine in the running
    loop (or runs it synchronously if no loop is present).
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_prune())
        logger.debug("prune_dead_urls: scheduled background prune task")
    except RuntimeError:
        # no running loop: run synchronously
        logger.debug("prune_dead_urls: running prune synchronously (no loop)")
        asyncio.run(_prune())
