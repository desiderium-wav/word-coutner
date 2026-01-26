import aiohttp
import time
import asyncio
from .db import connect
from .config import VERIFY_INTERVAL_DAYS, HTTP_TIMEOUT

async def _prune():
    cutoff = time.time() - VERIFY_INTERVAL_DAYS * 86400

    with connect() as db:
        c = db.cursor()
        c.execute("""
            SELECT id, url FROM media
            WHERE dead=0 AND last_verified < ?
        """, (int(cutoff),))
        rows = c.fetchall()

    if not rows:
        return

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as session:
        for mid, url in rows:
            try:
                # Use HEAD where possible; some servers may not respond to HEAD reliably.
                async with session.head(url) as r:
                    ok = r.status < 400
            except Exception:
                # try GET as a fallback if HEAD failed
                try:
                    async with session.get(url) as r:
                        ok = r.status < 400
                except Exception:
                    ok = False

            with connect() as db:
                c = db.cursor()
                c.execute("""
                    UPDATE media
                    SET dead=?, last_verified=?
                    WHERE id=?
                """, (0 if ok else 1, int(time.time()), mid))
                db.commit()

def prune_dead_urls():
    """
    Compatibility wrapper expected by main.py which calls this synchronously
    from an async task. This schedules the pruning coroutine in the running
    loop (or runs it synchronously if no loop is present).
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_prune())
    except RuntimeError:
        # no running loop: run synchronously (e.g., invoked from a script)
        asyncio.run(_prune())
