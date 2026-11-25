"""TradeMate's local execution journal.

Every write (place / modify / close) is recorded BEFORE the command is handed to MT5 and updated as it moves
through the pipeline. If TradeMate crashes, loses the answer, or the browser sees an HTTP 500, the journal still
says exactly how far the order got, so the real outcome can be reconciled against the account instead of guessed.

Stages (how far the command got) and states (what we know):

    stage  created   the command file was written for the bridge
           received  the bridge picked the file up (it disappeared)
           answered  the bridge answered with MT5's result

    state  pending               in flight
           not_sent              provably never reached MT5 (never written, or written and withdrawn unread)
           rejected              MT5/the broker refused it
           confirmed             the account itself shows the change
           unconfirmed           accepted by MT5 but not (yet) visible in the account
           unknown               sent, answer lost or an internal error: MUST be reconciled
           reconciled_executed   later checked against the account: it did happen
           reconciled_not_executed  later checked (or confirmed by the user): it did not happen

This is TradeMate's OWN record. Deleting it never touches MT5 or the broker's account history.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("trademate.journal")

UNRESOLVED_STATES = ("unknown", "unconfirmed")
FINAL_STATES = ("not_sent", "rejected", "confirmed", "reconciled_executed", "reconciled_not_executed")

_COLUMNS = ("id", "created_at", "updated_at", "expires_at", "action", "symbol", "side", "volume", "sl", "tp",
            "ticket", "cmd_id", "stage", "state", "message", "retcode", "before_tickets")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
    action TEXT NOT NULL, symbol TEXT, side TEXT, volume REAL, sl REAL, tp REAL, ticket INTEGER,
    cmd_id TEXT, stage TEXT NOT NULL DEFAULT 'none', state TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT '', retcode INTEGER, before_tickets TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS executions_created ON executions (created_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class JournalError(Exception):
    """The journal could not be read or written."""


class ExecutionJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db, db:      # closing(): sqlite3's own `with` does not close the connection
                db.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise JournalError(f"Cannot open the TradeMate journal at {self.path}: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _run(self, sql: str, args: tuple = (), many: bool = False):
        try:
            with self._lock, closing(self._connect()) as db:
                with db:                                  # transaction: commit on success, roll back on error
                    cur = db.execute(sql, args)
                    return cur.fetchall() if many else cur.rowcount
        except sqlite3.Error as exc:
            raise JournalError(f"Journal error: {exc}") from exc

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        data = {k: row[k] for k in _COLUMNS}
        data["before_tickets"] = json.loads(data["before_tickets"] or "[]")
        return data

    # ---------- writing ----------
    def begin(self, action: str, symbol=None, side=None, volume=None, sl=None, tp=None, ticket=None,
              before_tickets=(), expires_in: float = 30.0) -> str:
        """Record the intent. Called BEFORE anything is sent; if this fails the caller must not send."""
        now = time.time()
        exec_id = uuid.uuid4().hex[:12]
        self._run("INSERT INTO executions (id, created_at, updated_at, expires_at, action, symbol, side, volume, sl, tp,"
                  " ticket, before_tickets) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (exec_id, now, now, now + expires_in, action, symbol, side, volume, sl, tp, ticket,
                   json.dumps(sorted(int(t) for t in before_tickets))))
        return exec_id

    def update(self, exec_id: str, **fields) -> None:
        allowed = {"stage", "state", "message", "retcode", "ticket", "cmd_id", "symbol", "side", "volume", "sl", "tp"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Cannot update journal fields {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE executions SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), time.time(), exec_id))

    # ---------- reading ----------
    def get(self, exec_id: str) -> dict | None:
        rows = self._run("SELECT * FROM executions WHERE id = ?", (exec_id,), many=True)
        return self._row(rows[0]) if rows else None

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self._run("SELECT * FROM executions ORDER BY created_at DESC LIMIT ?", (int(limit),), many=True)
        return [self._row(r) for r in rows]

    def unresolved(self) -> list[dict]:
        marks = ",".join("?" for _ in UNRESOLVED_STATES)
        rows = self._run(f"SELECT * FROM executions WHERE state IN ({marks}) ORDER BY created_at", UNRESOLVED_STATES, many=True)
        return [self._row(r) for r in rows]

    def in_flight(self, within_seconds: float = 300.0) -> list[dict]:
        rows = self._run("SELECT * FROM executions WHERE state = 'pending' AND created_at >= ? ORDER BY created_at",
                         (time.time() - within_seconds,), many=True)
        return [self._row(r) for r in rows]

    def settle_stale(self, older_than: float = 120.0) -> int:
        """Records still 'pending' long after they should have finished (the process died mid-order) are UNKNOWN.

        Deliberately never 'not_sent': we cannot prove the command file did not reach MT5.
        """
        n = self._run("UPDATE executions SET state = 'unknown', updated_at = ?, message = ? "
                      "WHERE state = 'pending' AND created_at < ?",
                      (time.time(), "TradeMate stopped or lost track of this request while it was in flight. "
                                    "Check MT5, then reconcile it.", time.time() - older_than))
        if n:
            logger.error("%s order record(s) were left in flight; marked UNKNOWN for reconciliation.", n)
        return int(n)

    def count(self) -> int:
        return int(self._run("SELECT COUNT(*) AS n FROM executions", many=True)[0]["n"])

    # ---------- meta (small local settings, e.g. the history 'hide older than' marker) ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self._run("SELECT value FROM meta WHERE key = ?", (key,), many=True)
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._run("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, value))

    # ---------- deleting TradeMate's own history ----------
    def clear(self) -> int:
        """Delete every journal record and the local history marker. Local only: sends nothing to MT5."""
        deleted = self._run("DELETE FROM executions")
        self._run("DELETE FROM meta WHERE key = 'history_hidden_before'")
        logger.warning("TradeMate local history cleared (%s journal records deleted). MT5 was not contacted.", deleted)
        return int(deleted)
