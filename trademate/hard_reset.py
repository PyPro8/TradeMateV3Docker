"""Hard Reset: put the user's SETTINGS back to the documented defaults, safely and recoverably. It is NOT a database deletion.

What is reset (each file is first COPIED to data/backups/hard-reset-<time>/, then removed so the built-in defaults apply; nothing is overwritten in place):
    Command Centre trade settings, execution / safety switches, instrument & strategy selection, pip overrides, lot sizes, chart settings, Candle Momentum configuration,
    risk rules (percent limits). Browser-side display preferences are cleared by the page, never trade data.
What is NEVER touched (kept exactly as is): the trade-history database, risk records (daily risk log, consecutive-loss records, safety-lock history), notifications,
    the setups ledger (it prevents duplicate orders), the pipeline audit log, the Candle Momentum event log and live state, the Autopilot session record, the MT5 bridge files,
    every strategy implementation and every project file. Only files inside the data directory that are on the RESET list can be removed.
Refused while Autopilot is running or the emergency lock is set. Needs the typed phrase. Restore puts a backup's files back (the current ones are backed up first).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

PHRASE = "HARD RESET"
RESET_FILES = {
    "trade_settings.json": "Command Centre settings (instrument, lot, target and stop distances) for every scope",
    "execution_settings.json": "Execution and safety switches (multiple trades per candle, lot adjustment, consecutive-loss limit, sound)",
    "trade_selection.json": "Selected instrument and execution timeframes",
    "strategy_selection.json": "Selected strategy",
    "pip_sizes.json": "Your pip / point size overrides",
    "lot_sizes.json": "Saved lot sizes",
    "chart_settings.json": "Chart settings (palette, theme)",
    "chart_indicators.json": "Moving-average indicator settings",
    "bull_bear_antitwin.json": "Bull/Bear Anti-Twin ledger",
    "candle_momentum_antitwin.json": "Candle Momentum Anti-Twin ledger",
    "bull_bear_config.json": "Bull/Bear configuration (per instrument and timeframe)",
    "bull_bear_events.jsonl": "Bull/Bear event log",
    "bull_bear_state.json": "Bull/Bear live state",
    "candle_momentum_config.json": "Candle Momentum configuration (per instrument and timeframe)",
    "risk_rules.json": "Risk rule percentages (back to the configured defaults)",
}
KEPT = {
    "trade_journal.json": "Your trading journal (personal notes and emotions)",
    "manual_drawings.json": "Your manual chart lines",
    "trademate.sqlite3": "Trade history database",
    "risk_daily_log.json": "Historical daily risk records",
    "consecutive_losses.json": "Consecutive-loss records",
    "safety_lock.json": "Emergency lock and its history",
    "notifications.json": "Notification history",
    "setup_ledger.json": "Setups ledger (duplicate-order protection)",
    "pipeline_log.jsonl": "Pipeline audit log",
    "candle_momentum_events.jsonl": "Candle Momentum event log",
    "candle_momentum_state.json": "Candle Momentum live state",
    "autopilot_state.json": "Autopilot session record",
}


def plan(data_dir) -> dict:
    d = Path(data_dir)
    present = lambda names: [{"file": n, "what": w, "size": (d / n).stat().st_size} for n, w in names.items() if (d / n).is_file()]
    return {"will_reset": present(RESET_FILES), "kept": present(KEPT), "phrase": PHRASE, "backup_location": str(d / "backups"),
            "note": "Each reset file is copied to a backup folder first, then removed so the defaults apply. Trade history, risk records and all code are never touched. You can restore the backup."}


def perform(data_dir, confirm: str, *, autopilot_running: bool, locked: bool, now=time.time) -> tuple[bool, str, dict]:
    if confirm != PHRASE:
        return False, f"Type {PHRASE} to confirm.", {}
    if autopilot_running:
        return False, "Autopilot is running. Stop it first; a reset during a live session could leave a trade without its settings.", {}
    if locked:
        return False, "The emergency lock is set. Lift it first (the lock itself is not reset by a Hard Reset).", {}
    d = Path(data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    dest = d / "backups" / f"hard-reset-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = d / "backups" / f"hard-reset-{stamp}-{n}"
    moved = []
    files = [name for name in RESET_FILES if (d / name).is_file()]
    if files:
        dest.mkdir(parents=True)
        for name in files:                                                         # 1) copy everything; 2) only then remove: a failure leaves the originals in place
            shutil.copy2(d / name, dest / name)
        (dest / "manifest.json").write_text(json.dumps({"created": now(), "files": files}, indent=2), encoding="utf-8")
        for name in files:
            (d / name).unlink()
            moved.append(name)
    return True, (f"Reset {len(moved)} settings file(s). A backup is in {dest.name}." if moved else "There was nothing to reset: every setting was already at its default."), {"reset": moved, "backup": dest.name if moved else None}


def backups(data_dir) -> list:
    root = Path(data_dir) / "backups"
    out = []
    for p in sorted(root.glob("hard-reset-*"), reverse=True) if root.is_dir() else []:
        try:
            out.append({"name": p.name, **json.loads((p / "manifest.json").read_text(encoding="utf-8"))})
        except (OSError, ValueError):
            continue
    return out


def restore(data_dir, name: str, *, autopilot_running: bool, now=time.time) -> tuple[bool, str]:
    if autopilot_running:
        return False, "Autopilot is running. Stop it first."
    d = Path(data_dir)
    src = d / "backups" / name
    if not name.startswith("hard-reset-") or "/" in name or "\\" in name or not (src / "manifest.json").is_file():
        return False, "That backup does not exist."
    files = [f for f in json.loads((src / "manifest.json").read_text(encoding="utf-8")).get("files", []) if f in RESET_FILES and (src / f).is_file()]
    current = [f for f in files if (d / f).is_file()]
    if current:                                                                    # never overwrite without a backup of what is there now
        safe = d / "backups" / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))}"
        safe.mkdir(parents=True, exist_ok=True)
        for f in current:
            shutil.copy2(d / f, safe / f)
    for f in files:
        shutil.copy2(src / f, d / f)
    return True, f"Restored {len(files)} settings file(s) from {name}."
