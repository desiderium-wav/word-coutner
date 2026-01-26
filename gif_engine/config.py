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
# Candidate neko endpoints; some are SFW, some NSFW. We'll filter NSFW when allow_nsfw=False.
NEKOS_ENDPOINTS = os.getenv("NEKOS_ENDPOINTS", "ngif,neko,smug,pat,kiss,hug").split(",")
