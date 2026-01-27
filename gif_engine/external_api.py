import random
import logging
import aiohttp as i
from urllib.parse import quote_plus
from .config import (
    ENABLE_E621, E621_unn                     USERNAME, E621_API_KEY, E621_USER_AGENT,
    ENABLE_NEKOS, NEKOS_ENDPOINTS, HTTP_TIMEOUT,
    ENABLE_GIPHY, GIPHY_API_KEY, GIPHY_SEARCH_LIMIT,
    ENABLE_TENOR, TENOR_API_KEY, TENOR_cf        except Exception as e:
            logger.debug("nekos requestze de 7
                endpoints.remove(endpoint)
            except ValueError:
                pass
            continue

    return None

async def search_giphy(query: str, allow_nsfw: bool):
    if not ENABLE_GIPHY or not GIPHY_API_KEY:
        return None

    api_url = "https://api.giphy.com/v1/gifs/search"
    params = {
        "api_key": GIPHY_API_KEY,
        "q": query,
        "limit": str(GIPHY_SEARCH_LIMIT)
    }
    # Giphy rating param: 'g','pg','pg-13','r'. To avoid nsfw, request up to 'pg-13'
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

    data_list = data.get("data", [])
    candidates = []
    for item in data_list:
        images = item.get("images", {})
        original = images.get("original") or images.get("downsized") or {}
        url = original.get("url")
        if not url:
            continue
        rating = item.get("rating", "").lower()
        is_nsfw = rating in ("r",)
        if is_nsfw and not allow_nsfw:
            continue
        candidates.append({"url": url, "source": "giphy", "nsfw": is_nsfw})

    return random.choice(candidates) if candidates else None

async def search_tenor(query: str, allow_nsfw: bool):
    if not ENABLE_TENOR or not TENOR_API_KEY:
        return None

    api_url = "https://g.tenor.com/v1/search"
    params = {
        "q": query,
        "key": TENOR_API_KEY,
        "limit": str(TENOR_SEARCH_LIMIT)
    }
    # tenor contentfilter: off, low, medium, high (high is most restrictive)
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

    results = data.get("results", [])
    candidates = []
    for r in results:
        media_list = r.get("media", [])
        if not media_list:
            continue
        # Tenor media is a list of dicts with gif url under 'gif'->'url' typically
        first = media_list[0]
        if isinstance(first, dict):
            gif = first.get("gif") or first.get("mediumgif") or first.get("tinygif")
            if gif:
                url = gif.get("url")
            else:
                # fallback: r.get('url')
                url = r.get("we
        else:
            url = r.get("url")
        if not url:
            continue
        # Tenor doesn't provide an explicit rating field in v1 search responses here.
        # We'll be conservative: if contentfilter was high, treat as safe; otherwise unknown.
        is_nsfw = False
        if is_nsfw and not allow_nsfw:
            continue
        candidates.append({"url": url, "source": "tenor", "nsfw": is_nsfw})

    return random.choice(candidates) if candidates else None
