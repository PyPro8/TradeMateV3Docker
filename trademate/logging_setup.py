"""Basic logging: console plus a rotating log file."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from trademate.config import Settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_FILE_NAME = "trademate.log"


def setup_logging(settings: Settings) -> logging.Logger:
    """Configure the 'trademate' logger and return it.

    Modules should use logging.getLogger("trademate.<module>") so they
    inherit this configuration. Safe to call more than once.
    """
    settings.app.log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("trademate")
    logger.setLevel(settings.app.log_level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        settings.app.log_dir / LOG_FILE_NAME,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
