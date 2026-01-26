# gif_engine package initialization: set up a module logger with a reasonable default.
# Individual modules can get logging.getLogger(__name__).

import logging

# Configure a sensible default for library logging. Application can override.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

__all__ = []
