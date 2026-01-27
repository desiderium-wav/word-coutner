# gif_engine/external_api.py
# External provider integrations for GIF/image search.
# Each function returns either None or a dict: {"url": str, "source": str, "nsfw": bool}

import random
import logging
import aiohttp
from urllib.parse import quote_plus
from typing import Optional, Dict, Iterable

from .config import (
    ENABLE_E621, E621_USERNAME, E621_API_KEY, E621_USER_AGENT,
    ENABLE_NEKOS, NEKOS_ENDPOINTS, HTTP_TIMEOUT,
    ENABLE_GIPHY, GIPHY_API_KEY, GIPHY_SEARCH_LIMIT,
    ENABLE_TENOR, TENOR_API_KEY, TENOR_SEARCH_LIMIT,
)

logger = logging.getLogger(__name__)

# Helper: safe HTTP GET returning parsed json or None
async def _get_json(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, auth: aiohttp.BasicAuth | None = None):
    try:
        async with session.get(url, params=params, headers=headers, auth=auth) as resp:
            if resp.status != 200:
                logger.debug("HTTP GET %s returned status %s", url, resp.status)
                return None
            try:
                return await resp.json()
            except Exception as e:
                # Some endpoints respond with plain text; caller can handle
                logger.debug("Failed to parse JSON from %s: %s", url, e)
                return None
    except Exception as e:
        logger.debug("HTTP GET failed for %s: %s", url, e)
        return None

# E621: basic integration (requires username+api key or only API key depending on user config)
async def search_e621(query: str, allow_nsfw: bool) -> Optional[Dict]:
    if not ENABLE_E621 or not E621_API_KEY:
        return None

    api_url = "https://e621.net/posts.json"
    params = {"tags": query, "limit": "50"}
    headers = {"User-Agent": E621_USER_AGENT or "word-coutner-bot/1.0"}

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    auth = None
    if E621_USERNAME:
        auth = aiohttp.BasicAuth(E621_USERNAME, E621_API_KEY)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data = await _get_json(session, api_url, params=params, headers=headers, auth=auth)
            if not data:
                return None
            posts = data.get("posts") or []
            candidates = []
            for p in posts:
                fileinfo = p.get("file") or {}
                url = fileinfo.get("url")
                if not url:
                    continue
                rating = (p.get("rating") or "").lower()
                is_nsfw = rating in ("e",)  # e -> explicit
                if is_nsfw and not allow_nsfw:
                    continue
                candidates.append({"url": url, "source": "e621", "nsfw": is_nsfw})
            return random.choice(candidates) if candidates else None
    except Exception as e:
        logger.exception("e621 search error for query=%s: %s", query, e)
        return None

# Nekos / nekos.life style endpoints
async def search_nekos(query: str, allow_nsfw: bool) -> Optional[Dict]:
    if not ENABLE_NEKOS:
        return None

    endpoints: Iterable[str] = [e.strip() for e in NEKOS_ENDPOINTS if e and e.strip()]
    if not endpoints:
        return None

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # try endpoints in random order to broaden results
        for endpoint in random.sample(list(endpoints), k=len(endpoints)):
            # common nekos.life endpoint
            url1 = f"https://nekos.life/api/v2/img/{endpoint}"
            data = await _get_json(session, url1)
            if data and isinstance(data, dict):
                # typical response: {"url": "https://..."}
                url = data.get("url")
                if url:
                    # nerfed: nekos endpoints are usually SFW; we assume safe
                    return {"url": url, "source": f"nekos:{endpoint}", "nsfw": False}

            # some endpoints (ngif) use different APIs (nekos.best)
            url2 = f"https://nekos.best/api/v2/{endpoint}"
            data = await _get_json(session, url2)
            if data and isinstance(data, dict):
                # structure: {'results': [{'url': '...', ...}, ...]}
                results = data.get("results") or []
                if results:
                    r = random.choice(results)
                    url = r.get("url") or r.get("file") or r.get("image")
                    if url:
                        return {"url": url, "source": f"nekos.best:{endpoint}", "nsfw": False}
            # if both failed, continue to next endpoint
    return None

# Giphy integration
async def search_giphy(query: str, allow_nsfw: bool) -> Optional[Dict]:
    if not ENABLE_GIPHY or not GIPHY_API_KEY:
        return None

    api_url = "https://api.giphy.com/v1/gifs/search"
    params = {
        "api_key": GIPHY_API_KEY,
        "q": query,
        "limit": str(GIPHY_SEARCH_LIMIT),
    }
    # To avoid NSFW, restrict rating when not allowed
    if not allow_nsfw:
        params["rating"] = "pg-13"

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(api_url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Giphy search failed status=%s for %s", resp.status, query)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.exception("Giphy request failed: %s", e)
        return None

    data_list = data.get("data", []) or []
    candidates = []
    for item in data_list:
        images = item.get("images", {}) or {}
        original = images.get("original") or images.get("downsized") or {}
        url = original.get("url")
        if not url:
            continue
        rating = (item.get("rating") or "").lower()
        is_nsfw = rating in ("r",)
        if is_nsfw and not allow_nsfw:
            continue
        candidates.append({"url": url, "source": "giphy", "nsfw": is_nsfw})

    return random.choice(candidates) if candidates else None

# Tenor integration
async def search_tenor(query: str, allow_nsfw: bool) -> Optional[Dict]:
    if not ENABLE_TENOR or not TENOR_API_KEY:
        return None

    api_url = "https://g.tenor.com/v1/search"
    params = {
        "q": query,
        "key": TENOR_API_KEY,
        "limit": str(TENOR_SEARCH_LIMIT),
    }
    # Tenor contentfilter (low/medium/high); high = most restrictive
    params["contentfilter"] = "low" if allow_nsfw else "high"

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(api_url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Tenor search failed status=%s for %s", resp.status, query)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.exception("Tenor request failed: %s", e)
        return None

    results = data.get("results", []) or []
    candidates = []
    for r in results:
        media_list = r.get("media", []) or []
        # Tenor media entries are lists of dicts, attempt to extract gif url
        if not media_list:
            # older response shape: r.get('url')
            url = r.get("url")
            if url:
                candidates.append({"url": url, "source": "tenor", "nsfw": False})
            continue

        first = media_list[0]
        # first can be dict keyed by types like 'gif', 'mediumgif', 'tinygif'
        if isinstance(first, dict):
            gif_obj = first.get("gif") or first.get("mediumgif") or first.get("tinygif")
            if gif_obj and isinstance(gif_obj, dict):
                url = gif_obj.get("url")
            else:
                # sometimes nested differently
                url = None
                for v in first.values():
                    if isinstance(v, dict) and v.get("url"):
                        url = v.get("url")
                        break
        else:
            # fallback
            url = r.get("url")

        if not url:
            continue

        # Tenor does not reliably tag NSFW in v1 responses; assume False.
        is_nsfw = False
        if is_nsfw and not allow_nsfw:
            continue
        candidates.append({"url": url, "source": "tenor", "nsfw": is_nsfw})

    return random.choice(candidates) if candidates else None
