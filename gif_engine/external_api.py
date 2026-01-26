import random
import logging
import aiohttp
from urllib.parse import quote_plus
from .config import (
    ENABLE_E621, E621_USERNAME, E621_API_KEY, E621_USER_AGENT,
    ENABLE_NEKOS, NEKOS_ENDPOINTS, HTTP_TIMEOUT
)

logger = logging.getLogger(__name__)

async def search_e621(query: str, allow_nsfw: bool):
    """
    Query e621 for posts that match `query` (as tags).
    Returns dict {url, source, nsfw} or None.
    """
    if not ENABLE_E621:
        return None

    base = "https://e621.net"
    posts_path = "/posts.json"

    # Convert query to tags for e621 (replace spaces with underscores)
    tags = quote_plus(query.replace(" ", "_"))
    # rating filter if user doesn't allow nsfw (safe only)
    if not allow_nsfw:
        tags = f"{tags}+rating:s"

    params = f"?tags={tags}&limit=100"
    url = base + posts_path + params

    headers = {"User-Agent": E621_USER_AGENT}
    auth = None
    if E621_USERNAME and E621_API_KEY:
        # aiohttp BasicAuth will encode credentials
        auth = aiohttp.BasicAuth(E621_USERNAME, E621_API_KEY)

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    try:
        async with aiohttp.ClientSession(auth=auth, timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("e621 search failed status=%s for %s", resp.status, query)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.exception("e621 request failed: %s", e)
        return None

    posts = data.get("posts", [])
    if not posts:
        return None

    # Filter posts which have file.url
    candidates = []
    for p in posts:
        fileinfo = p.get("file", {})
        file_url = fileinfo.get("url")
        if not file_url:
            continue
        rating = p.get("rating")
        is_nsfw = (rating != "s")
        candidates.append({"url": file_url, "source": "e621", "nsfw": is_nsfw})

    if not candidates:
        return None

    return random.choice(candidates)

async def search_nekos(query: str, allow_nsfw: bool):
    """
    Query nekos.life random endpoints. nekos.life doesn't support search by query;
    we'll pick an endpoint and request an image. Returns dict or None.
    Note: NEKOS endpoints differ in nsfw level; NEKOS_ENDPOINTS can be tuned.
    """
    if not ENABLE_NEKOS:
        return None

    # pick endpoints and filter out obviously NSFW ones when nsfw disabled
    endpoints = list(NEKOS_ENDPOINTS)
    if not endpoints:
        return None

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    for _ in range(len(endpoints)):
        endpoint = random.choice(endpoints)
        api_url = f"https://nekos.life/api/v2/img/{endpoint}"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(api_url) as resp:
                    if resp.status != 200:
                        logger.debug("nekos endpoint %s returned %s", endpoint, resp.status)
                        endpoints.remove(endpoint)
                        continue
                    data = await resp.json()
                    url = data.get("url")
                    if not url:
                        endpoints.remove(endpoint)
                        continue
                    # We don't have explicit rating info from nekos.life. Heuristics:
                    is_nsfw = endpoint.lower().startswith(("lewd", "hentai", "nsfw"))
                    if is_nsfw and not allow_nsfw:
                        # skip and try another endpoint
                        endpoints.remove(endpoint)
                        continue
                    return {"url": url, "source": f"nekos.{endpoint}", "nsfw": is_nsfw}
        except Exception as e:
            logger.debug("nekos request failed for %s: %s", endpoint, e)
            try:
                endpoints.remove(endpoint)
            except ValueError:
                pass
            continue

    return None
