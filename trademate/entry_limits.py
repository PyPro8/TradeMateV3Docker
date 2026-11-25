"""Entries per candle: one (default) or at most two DISTINCT ones, with protection against accidental duplicates.

A setup's ledger id is its candidate id (strategy + direction + timeframe + candle ...). Entry 1 uses the id itself, entry 2 uses "<id>#2".
A second entry is a LEGITIMATE new entry only when ALL of these hold:
  * the setting allows two entries per candle;
  * entry 1 was really submitted (claimed in the ledger: a pending approval is not an entry);
  * the approval for entry 2 was CREATED AFTER entry 1 was claimed. An approval created before that (a double click, a re-sent request, a second
    browser tab, a parent trade copied to children) is a REPEATED REQUEST for the same entry and is refused.
Never more than two, whatever the setting says. Strategy, instrument and timeframe are part of every candidate id, so they never share a count.
"""

from __future__ import annotations

HARD_MAX = 2


def slot_id(cid: str, n: int) -> str:
    return cid if n == 1 else f"{cid}#{n}"


def next_slot(ledger, cid: str, max_entries: int, approval_created_at: float | None = None):
    """-> (ledger id to claim, entry number) or (None, reason). `approval_created_at=None` (a proposal) only asks whether another entry is possible at all."""
    if ledger is None or not cid:
        return cid, 1
    limit = max(1, min(int(max_entries or 1), HARD_MAX))
    for n in range(1, limit + 1):
        sid = slot_id(cid, n)
        if ledger.is_taken(sid):
            continue
        if n == 1:
            return sid, 1
        prev = ledger.get(slot_id(cid, n - 1)) or {}
        if approval_created_at is not None and approval_created_at <= float(prev.get("at", 0.0)):
            return None, "repeated request: this approval was created before the previous entry on this candle was submitted"
        return sid, n
    if limit == 1:
        return None, "An order was already submitted for this exact setup. One setup, one order."
    return None, f"The maximum of {limit} entries for this candle has been used."


def entry_no(cid: str, slot: str) -> int:
    return 2 if slot != cid else 1
