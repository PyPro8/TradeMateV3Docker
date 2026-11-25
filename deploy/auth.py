"""WSGI Basic Authentication and same-origin write guard for Render."""
from __future__ import annotations

import base64
import binascii
import hmac
from urllib.parse import urlsplit


class BasicAuth:
    def __init__(self, application, username: str, password: str):
        self.application = application
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")

    @staticmethod
    def _respond(start_response, status: str, body: bytes, headers=None):
        base = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if headers:
            base.extend(headers)
        start_response(status, base)
        return [body]

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        if method == "GET" and path == "/healthz":
            return self._respond(start_response, "200 OK", b"ok\n")

        supplied = environ.get("HTTP_AUTHORIZATION", "")
        valid = False
        if supplied.startswith("Basic "):
            try:
                raw = base64.b64decode(supplied[6:], validate=True)
                user, separator, password = raw.partition(b":")
                valid = bool(separator) and hmac.compare_digest(user, self.username) and hmac.compare_digest(password, self.password)
            except (binascii.Error, ValueError):
                valid = False
        if not valid:
            return self._respond(
                start_response,
                "401 Unauthorized",
                b"Authentication required.\n",
                [("WWW-Authenticate", 'Basic realm="TradeMate", charset="UTF-8"')],
            )

        # Reject cross-origin browser writes. Missing Origin remains allowed for
        # authenticated command-line clients; browsers send Origin on POSTs.
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = environ.get("HTTP_ORIGIN")
            if origin:
                try:
                    origin_host = (urlsplit(origin).hostname or "").lower()
                    request_host = (urlsplit("//" + environ.get("HTTP_HOST", "")).hostname or "").lower()
                except ValueError:
                    origin_host = ""
                    request_host = ""
                if not origin_host or not request_host or not hmac.compare_digest(origin_host, request_host):
                    return self._respond(start_response, "403 Forbidden", b"Cross-origin request refused.\n")

        return self.application(environ, start_response)
