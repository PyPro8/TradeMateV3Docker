"""One place that decides whether the connected MT5 server is the one TradeMate expects.

Pure function, no I/O, used by the adapter's order guard AND by the opt-in real-demo scripts so the rule can never
drift. FAIL CLOSED: an order is only allowed when the expected server is configured, the connected server is known, and
the two are the same name. Only harmless whitespace and letter-case differences are ignored; there is no substring or
prefix matching, so "Headway-Demo2" or "NotHeadway-Demo" do NOT match "Headway-Demo".
"""

from __future__ import annotations

DEFAULT_EXPECTED_SERVER = "Headway-Demo"


def normalise_server(name: object) -> str:
    """Trim and collapse whitespace, ignore letter case. Nothing else is normalised."""
    return " ".join(str(name or "").split()).casefold()


def check_server(expected: object, connected: object) -> tuple[bool, str]:
    """(allowed, reason). `reason` is always safe to show or log: it contains server NAMES only, never credentials."""
    want, got = normalise_server(expected), normalise_server(connected)
    if not want:
        return False, ("No expected MT5 server is configured, so the server cannot be verified. Set [mt5] expected_server "
                       f"(or TRADEMATE_EXPECTED_SERVER) to {DEFAULT_EXPECTED_SERVER!r}.")
    if not got:
        return False, "MT5 did not report which server the account is on, so the server cannot be verified."
    if want != got:
        return False, f"The MT5 account is on server {str(connected).strip()!r}, but TradeMate expects {str(expected).strip()!r}."
    return True, f"Server {str(connected).strip()!r} matches the expected server."
