import aiohttp
import time
from .db import connect
from .config import VERIFY_INTERVAL_DAYS, HTTP_TIMEOUT

async def prune():
    cutoff = time.time() - VERIFY_INTERVAL_DAYS * 86400

    with connect() as db:
        c = db.cursor()
        c.execute("""
            SELECT id, url FROM media
            WHERE dead=0 AND last_verified < ?
        """, (int(cutoff),))
        rows = c.fetchall()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as session:
        for mid, url in rows:
            try:
                async with session.head(url) as r:
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
