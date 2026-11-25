"""Diagnostic trace: dump every swing, reference level and event the structure engine produces for a candle file.

Use it to capture a live chart (for example CRASH_100 H1) and audit a confusing event sequence. Read-only; no broker, no orders.

    python3 -m trademate.structure.trace candles.json [--left 3] [--right 3] [--mode close|wick] [--forming]

candles.json: a JSON list of [time, open, high, low, close] rows (or objects with those keys), oldest first. Export it from the
app with /api/candles?symbol=CRASH_100&timeframe=H1&count=500 (the "candles" field). Pass --forming if the newest candle is open.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from trademate.structure.engine import StructureConfig, analyze_market_structure, clean_candles


def _ts(t) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t is not None else "-"


def render(candles, cfg: StructureConfig) -> str:
    res = analyze_market_structure(candles, cfg)
    bars, _ = clean_candles(candles)
    out = [f"config: {cfg.to_dict()}", f"candles used: {res.diagnostics.used_count} of {res.diagnostics.input_count}"]
    out += [f"warning: {w}" for w in res.diagnostics.warnings]
    out.append("--- swings (pivot, confirmed, kind, price, class) ---")
    for s in res.swings:
        out.append(f"{_ts(s.pivot_time)}  conf {_ts(s.confirmed_time)}  {s.kind:<4} {s.price:<12g} {s.classification or '-'}"
                   + ("" if s.confirmed else "  (provisional)"))
    out.append("--- events ---")
    for e in res.events:
        out.append(f"{_ts(e.candle_time)}  {e.type:<5} {e.direction:<7} level {e.level:g} (ref {e.reference_swing_id} "
                   f"{e.reference_classification or '-'}, role {e.reference_role})  {e.trend_before or 'none'} -> {e.trend_after or 'none'}  "
                   f"{'CONFIRMED' if e.confirmed else 'PROVISIONAL'}{'  SAME-CANDLE-CONFLICT' if e.same_candle_conflict else ''}")
        out.append(f"      why: {e.reason}")
    out.append("--- final state ---")
    for name in ("trend_confirmed", "trend"):
        out.append(f"{name}: {getattr(res, name)}")
    for name in ("protected_high", "protected_low", "continuation_high", "continuation_low", "active_high", "active_low"):
        sw = getattr(res, name)
        out.append(f"{name}: {f'{sw.price:g} @ {_ts(sw.pivot_time)} {sw.classification or chr(45)}' if sw else None}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--left", type=int, default=3)
    ap.add_argument("--right", type=int, default=3)
    ap.add_argument("--mode", default="close", choices=("close", "wick"))
    ap.add_argument("--forming", action="store_true", help="the newest candle is still open")
    a = ap.parse_args(argv)
    try:
        with open(a.file, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("candles", [])
        cfg = StructureConfig(swing_left=a.left, swing_right=a.right, break_mode=a.mode, last_candle_closed=not a.forming)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render(data, cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
