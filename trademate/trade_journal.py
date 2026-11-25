"""The TRADING JOURNAL: the user's personal trading notepad. (Not the execution journal in trademate/journal.py, which records every order TradeMate sent.)

An entry holds what the person writes: date, time, instrument, strategy, result, profit or loss, emotional state and free notes. Those are USER FIELDS: only
the user edits them, and nothing automatic ever overwrites them.

An entry may also carry an `auto` block: facts about a trade copied from TradeMate (for Rapid Fire: ticket, side, entry/exit price, realised net, how it closed).
Importing or refreshing a Rapid Fire trade only ever (re)writes the `auto` block of an entry and creates new entries with EMPTY emotion and notes; it can never
touch the user's words. Nothing here trades, executes or changes any strategy.

Stored in one JSON file, written atomically (temp file + replace).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

RESULTS = ("", "win", "loss", "breakeven", "open")
EMOTIONS = ("", "calm", "confident", "excited", "anxious", "fearful", "greedy", "frustrated", "impatient", "bored", "tired", "disciplined", "regretful", "neutral", "other")
USER_FIELDS = ("date", "time", "instrument", "strategy", "result", "profit_loss", "emotion", "notes")
MAX_ENTRIES = 5000
LIMITS = {"instrument": 40, "strategy": 60, "emotion": 40, "notes": 5000}


def _clean_text(v, name, problems, limit):
    if v is None:
        return ""
    if not isinstance(v, str):
        problems.append(f"{name} must be text.")
        return ""
    v = v.strip()
    if len(v) > limit:
        problems.append(f"{name} is too long (at most {limit} characters).")
    return v


def validate_fields(data: dict, *, partial: bool) -> tuple[dict, list[str]]:
    """Check the user fields. `partial` = an edit: only the keys that are present are checked and returned."""
    problems: list[str] = []
    out: dict = {}
    if not isinstance(data, dict):
        return {}, ["The entry must be a JSON object."]
    unknown = sorted(k for k in data if k not in USER_FIELDS)
    if unknown:
        problems.append("Unknown field(s): " + ", ".join(unknown) + ".")
    for k in USER_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "date":
            v = "" if v is None else v
            if not isinstance(v, str) or (v.strip() and not _valid_date(v.strip())):
                problems.append("Date must be YYYY-MM-DD.")
                continue
            out[k] = v.strip()
        elif k == "time":
            v = "" if v is None else v
            if not isinstance(v, str) or (v.strip() and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v.strip())):
                problems.append("Time must be HH:MM (24 hour).")
                continue
            out[k] = v.strip()
        elif k == "result":
            v = "" if v is None else v
            if v not in RESULTS:
                problems.append("Result must be one of: win, loss, breakeven, open (or empty).")
                continue
            out[k] = v
        elif k == "profit_loss":
            if v is None or (isinstance(v, str) and v.strip() == ""):
                out[k] = None
            else:
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    problems.append("Profit or loss must be a number.")
                    continue
                if isinstance(v, bool) or n != n or n in (float("inf"), float("-inf")):
                    problems.append("Profit or loss must be a number.")
                    continue
                out[k] = n
        else:
            out[k] = _clean_text(v, k.capitalize(), problems, LIMITS[k])
    if not partial:
        for k in USER_FIELDS:
            out.setdefault(k, None if k == "profit_loss" else "")
        if not any(out.get(k) not in ("", None) for k in ("instrument", "strategy", "result", "profit_loss", "emotion", "notes")):
            problems.append("Write at least one thing in the entry (instrument, result, emotion or notes).")
    return out, problems


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


class TradeJournal:
    def __init__(self, path, clock=time.time):
        self.path, self._clock = Path(path), clock
        self._lock = threading.RLock()

    # ---- storage
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict) and e.get("id")] if isinstance(entries, list) else []

    def _save(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "entries": entries}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- API
    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._load(), key=lambda e: (e.get("date") or "", e.get("time") or "", e.get("created_at", 0)), reverse=True)

    def get(self, eid: str) -> dict | None:
        with self._lock:
            return next((e for e in self._load() if e["id"] == eid), None)

    def create(self, data: dict, *, auto: dict | None = None) -> tuple[dict | None, list[str]]:
        fields, problems = validate_fields(data, partial=False)
        if problems:
            return None, problems
        with self._lock:
            entries = self._load()
            if len(entries) >= MAX_ENTRIES:
                return None, [f"The journal is full ({MAX_ENTRIES} entries). Delete some first."]
            now = self._clock()
            entry = {"id": "j-" + uuid.uuid4().hex[:12], "created_at": now, "updated_at": now, **fields}
            if auto:
                entry["auto"] = dict(auto)
            entries.append(entry)
            self._save(entries)
            return entry, []

    def update(self, eid: str, data: dict) -> tuple[dict | None, list[str]]:
        """Edit the user's own fields. The automatic `auto` block cannot be edited here."""
        fields, problems = validate_fields(data, partial=True)
        if problems:
            return None, problems
        with self._lock:
            entries = self._load()
            for e in entries:
                if e["id"] == eid:
                    e.update(fields)
                    e["updated_at"] = self._clock()
                    self._save(entries)
                    return e, []
        return None, ["That journal entry does not exist."]

    def delete(self, eid: str) -> bool:
        with self._lock:
            entries = self._load()
            kept = [e for e in entries if e["id"] != eid]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True

    # ---- Rapid Fire trades: documented, never executed
    def import_rapid_fire(self, rows: list[dict]) -> dict:
        """Add every CLOSED Rapid Fire trade that is not in the journal yet (empty emotion and notes for the user to fill in), and refresh the `auto` facts of
        those already there. The user's own fields of an existing entry are never touched."""
        created = refreshed = 0
        with self._lock:
            entries = self._load()
            by_ticket = {(e.get("auto") or {}).get("ticket"): e for e in entries if (e.get("auto") or {}).get("source") == "rapid_fire" and (e.get("auto") or {}).get("ticket") is not None}
            for r in sorted(rows, key=lambda r: r.get("entry_time") or 0):
                if r.get("execution_status") != "CLOSED" or r.get("ticket") is None:
                    continue
                auto = {"source": "rapid_fire", "ticket": r["ticket"], "role": r.get("role_label"), "side": r.get("side"), "volume": r.get("volume"), "entry_price": r.get("entry_price"),
                        "exit_price": r.get("exit_price"), "gross": r.get("gross"), "commission": r.get("commission"), "swap": r.get("swap"), "net": r.get("net"), "closed_by": r.get("closed_by"),
                        "twin_of_ticket": r.get("twin_of_ticket"), "target_points": r.get("target_points")}
                if r["ticket"] in by_ticket:
                    by_ticket[r["ticket"]]["auto"] = auto                       # facts only
                    refreshed += 1
                    continue
                if len(entries) >= MAX_ENTRIES:
                    break
                t = r.get("entry_time")
                dt = datetime.fromtimestamp(t, tz=timezone.utc) if isinstance(t, (int, float)) else None
                net = r.get("net")
                now = self._clock()
                entries.append({"id": "j-" + uuid.uuid4().hex[:12], "created_at": now, "updated_at": now, "date": dt.strftime("%Y-%m-%d") if dt else "", "time": dt.strftime("%H:%M") if dt else "",
                                "instrument": r.get("symbol") or "", "strategy": "Rapid Fire" + (" (Anti-Twin)" if r.get("role_label") == "ANTI-TWIN" else ""),
                                "result": "" if net is None else ("win" if net > 0.004 else "loss" if net < -0.004 else "breakeven"), "profit_loss": net, "emotion": "", "notes": "", "auto": auto})
                created += 1
            if created or refreshed:
                self._save(entries)
        return {"created": created, "refreshed": refreshed}
