import logging
import os

from .config import IMAGE_SAVE_DIRECTORY


def configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def ensure_runtime_directories() -> None:
    os.makedirs(IMAGE_SAVE_DIRECTORY, exist_ok=True)
