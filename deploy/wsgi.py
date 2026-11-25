"""Production WSGI entry point with mandatory HTTP Basic authentication."""
from __future__ import annotations

import os

from app import app as flask_app
from deploy.auth import BasicAuth


def create_application():
    username = os.environ.get("TRADEMATE_WEB_USER", "").strip()
    password = os.environ.get("TRADEMATE_WEB_PASSWORD", "")
    if not username or len(password) < 16:
        raise RuntimeError(
            "Set TRADEMATE_WEB_USER and TRADEMATE_WEB_PASSWORD (at least 16 characters) in Render secrets before starting TradeMate."
        )
    return BasicAuth(flask_app, username, password)


application = create_application()
