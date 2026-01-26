import os

DB_PATH = "gif_engine/gifs.db"

EMBEDDING_DIM = 384  # MiniLM

SIMILARITY_THRESHOLD = 0.55
MAX_RESULTS = 25

VERIFY_INTERVAL_DAYS = 14
HTTP_TIMEOUT = 10

# External API integration toggles and config
ENABLE_E621 = os.getenv("ENABLE_E621", "false").lower() in ("1", "true", "yes")
E621_USERNAME = os.getenv("E621_USERNAME", None)
E621_API_KEY = os.getenv("E621_API_KEY", None)
E621_USER_AGENT = os.getenv("E621_USER_AGENT", "word-coutner-bot/1.0 (by username)")

ENABLE_NEKOS = os.getenv("ENABLE_NEKOS", "true").lower() in ("1", "true", "yes")
NEKOS_ENDPOINTS = os.getenv("NEKOS_ENDPOINTS", "ngif,neko,smug,pat,kiss,hug").split(",")

ENABLE_GIPHY = os.getenv("ENABLE_GIPHY", "false").lower() in ("1", "true", "yes")
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", None)
GIPHY_SEARCH_LIMIT = int(os.getenv("GIPHY_SEARCH_LIMIT", "50"))

ENABLE_TENOR = os.getenv("ENABLE_TENOR", "false").lower() in ("1", "true", "yes")
TENOR_API_KEY = os.getenv("TENOR_API_KEY", None)
TENOR_SEARCH_LIMIT = int(os.getenv("TENOR_SEARCH_LIMIT", "50"))

# If True, external APIs/cache are consulted before local DB
PREFER_EXTERNAL = os.getenv("PREFER_EXTERNAL", "false").lower() in ("1", "true", "yes")
# Order to try external APIs when falling back
EXTERNAL_API_ORDER = os.getenv("EXTERNAL_API_ORDER", "e621,giphy,tenor,nekos").split(",")

# Cache TTL for external API results (days)
CACHE_TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "7"))
