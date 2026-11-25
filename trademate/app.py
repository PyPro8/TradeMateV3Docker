"""Application entry point and top-level error handling."""

from __future__ import annotations

import logging
import sys

from trademate import __version__
from trademate.config import Settings, load_config
from trademate.errors import ConfigError, TradeMateError
from trademate.logging_setup import setup_logging

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2


def initialize(settings: Settings | None = None) -> Settings:
    """Load configuration (unless provided) and set up logging."""
    if settings is None:
        settings = load_config()
    logger = setup_logging(settings)
    logger.info("TradeMate %s starting in %s mode", __version__, settings.mode.value)
    return settings


def main() -> int:
    """Run TradeMate. Returns a process exit code."""
    try:
        settings = initialize()
        print(settings.app.name)
        print("Foundation initialized successfully.")
        print(f"Mode: {settings.mode.value}")
        logging.getLogger("trademate.app").info("Foundation initialized successfully.")
        return EXIT_OK
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except TradeMateError as exc:
        logging.getLogger("trademate.app").error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception:
        logging.getLogger("trademate.app").exception("Unexpected error")
        print("Unexpected error. See logs/trademate.log for details.", file=sys.stderr)
        return EXIT_ERROR
