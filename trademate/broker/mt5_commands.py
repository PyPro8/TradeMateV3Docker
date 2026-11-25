"""Command channel: TradeMate -> MT5 (the WRITE direction).

How it works
------------
TradeMate writes one small text file, ``trademate_cmd_<id>.txt``, into MT5's
shared Common/Files folder (written under a temp name and renamed, so MT5 never
sees a half-written file). The TradeMateBridge EA inside MT5 polls for those
files, executes the command with the MT5 trade API, and answers with
``trademate_res_<id>.json`` holding the broker's real return code.

Safety properties:
  * every command carries an expiry time; the EA ignores expired commands, so an
    order can never fire late because MT5 was down when it was sent;
  * if no answer arrives, TradeMate tries to withdraw the command file. If that
    succeeds, the command provably never ran; if not, the outcome is UNKNOWN and
    the caller must say so instead of claiming success.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

from trademate.errors import BrokerUnavailableError, CommandWithdrawnError, ExecutionUnconfirmedError

logger = logging.getLogger("trademate.broker.mt5.commands")

POLL_SECONDS = 0.05


def _clean(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


class CommandChannel:
    def __init__(self, directory: Path):
        self.directory = directory

    def send(self, fields: dict, timeout: float, expires_in: float | None = None, on_stage=None) -> dict:
        """Send a command and wait for MT5's JSON answer. Raises on timeout (never guesses).

        ``on_stage(stage, cmd_id)`` is told how far the command got: "created" (file written), "received" (the
        bridge took the file) and "answered". It is how the order journal knows the truth if the answer is lost.
        A failing callback is logged and never breaks the send.
        """
        def stage(name):
            if on_stage is None:
                return
            try:
                on_stage(name, cmd_id)
            except Exception:  # the journal must never stop or alter a send that is already under way
                logger.exception("Command %s: stage callback failed at '%s'", cmd_id, name)

        cmd_id = uuid.uuid4().hex[:16]
        fields = dict(fields)
        fields["id"] = cmd_id
        # The EA reads a whole number of seconds. Round UP: int() truncated, so a command could expire up to a full second
        # EARLIER than requested (a 2 s window could be ~1 s), which made commands expire under load.
        fields["expires"] = math.ceil(time.time() + (expires_in if expires_in is not None else timeout))
        body = "".join(f"{k}={_clean(v)}\n" for k, v in fields.items())

        tmp = self.directory / f"trademate_cmd_{cmd_id}.tmp"
        final = self.directory / f"trademate_cmd_{cmd_id}.txt"
        result_path = self.directory / f"trademate_res_{cmd_id}.json"
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, final)
        except OSError as exc:
            raise BrokerUnavailableError(f"Could not write the command for MT5: {exc}") from exc
        logger.info("Command %s sent: %s", cmd_id, {k: v for k, v in fields.items() if k != "expires"})
        stage("created")

        deadline = time.monotonic() + timeout
        received = False
        while time.monotonic() < deadline:
            if result_path.exists():
                result = self._read_result(result_path)
                if result is not None:
                    logger.info("Command %s answered: %s", cmd_id, {k: v for k, v in result.items() if k != "bars"})
                    if not received:
                        stage("received")
                    stage("answered")
                    return result
            if not received and not final.exists():
                received = True        # the bridge took the file: from here on it may be executing
                stage("received")
            time.sleep(POLL_SECONDS)

        # No answer. Try to withdraw the command so it cannot run later.
        try:
            final.unlink()
            logger.warning("Command %s: no answer and the file was withdrawn unread: it never reached MT5.", cmd_id)
            raise CommandWithdrawnError(
                "MT5 did not answer in time and the command was withdrawn, so nothing was sent to the broker. "
                "Is MT5 running with the TradeMateBridge EA attached?")
        except FileNotFoundError:
            pass  # MT5 already took it: it may have run
        except OSError:
            pass
        # Last look in case the answer landed while we were withdrawing.
        result = self._read_result(result_path) if result_path.exists() else None
        if result is not None:
            stage("received")
            stage("answered")
            return result
        logger.error("Command %s: MT5 took the command but no answer came back. Outcome UNKNOWN.", cmd_id)
        stage("received")
        raise ExecutionUnconfirmedError(
            "MT5 picked up the command but no confirmation came back. The outcome is UNKNOWN: "
            "TradeMate will check the account; do not send the same order again until that is resolved.")

    def _read_result(self, path: Path) -> dict | None:
        for _ in range(3):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            try:
                path.unlink()
            except OSError:
                pass
            return data if isinstance(data, dict) else None
        return None
