"""
A simple smoke test script for the gif_engine package.

- Initializes the gif DB
- Ingests a sample query/media
- Runs an async search for that query
- Schedules a prune run (non-blocking when run within an event loop)
"""

import asyncio
import time
from gif_engine.db import init_gif_db
from gif_engine.ingest import ingest_result
from gif_engine.search import search_gifs
from gif_engine.prune import prune_dead_urls

def main():
    print("Initializing gif DB...")
    init_gif_db()

    q = "funny cat"
    url = "https://example.com/funny-cat.gif"
    source = "smoke_test"
    print("Ingesting sample media...")
    ingest_result(q, url, source, nsfw=False)

    async def run_search_and_prune():
        print("Searching for 'funny cat' ...")
        res = await search_gifs("funny cat", allow_nsfw=False)
        print("Search result:", res)
        # Schedule prune (non-blocking)
        prune_dead_urls()
        # give prune a bit of time to run in background
        await asyncio.sleep(2)

    asyncio.run(run_search_and_prune())
    print("Smoke test finished.")

if __name__ == "__main__":
    main()
