"""Allows `python -m trademate`."""

import sys

from trademate.app import main

if __name__ == "__main__":
    sys.exit(main())
