"""Strategy: Trendline Trading (id "trendline").  RULE "TRENDLINE-v1".   One timeframe: the user's execution timeframe.   Independent of Market Structure.

Bearish  the two most recent confirmed swing highs are descending (lower highs). A resistance line is drawn through them. The line is INTACT while no closed candle after the
         second high has closed above it. SELL when the newest closed candle closes below the line (price is respecting the line).
Bullish  the mirror: the two most recent confirmed swing lows are ascending; a support line through them; BUY when the newest closed candle closes above it with the line intact.
Target   the indicated level: a parallel line through the extreme opposite swing between the two anchors (the lowest swing low for a bearish line, the highest swing high for a
         bullish one), projected to the signal candle. If that level is not beyond the entry there is no target and no trade.
Stop     beyond the second anchor swing (BUY: below it; SELL: above it) plus the configured buffer. Needs reward-to-risk >= params.min_rr.
The drawn lines are returned in result["trendlines"] (resistance/support, anchors, projected level) for the chart. Manual drawings are separate and never read here.
"""

from __future__ import annotations

from trademate.strategies._single_tf import Run, closed_rows_problem
from trademate.strategies.base import BEAR, BULL, confirmed_swings, run_analyses


def line_at(a, b, t):
    """Value at time t of the straight line through swings a and b (each {"time","price"})."""
    if b["time"] == a["time"]:
        return None
    return a["price"] + (b["price"] - a["price"]) * (t - a["time"]) / (b["time"] - a["time"])


def evaluate(defn, symbol, series, params, cfg, clock):
    r = Run(defn, symbol, series, params, clock)
    if r.problems:
        return r.unusable()
    tf = r.tf
    s = series[tf]
    rows = list(s.closed)
    now = clock.get("now_server") if isinstance(clock, dict) else None
    bad = closed_rows_problem(rows, 20, tf, s.newest_closed_end, now)
    if bad:
        return r.wait("history", bad, f"More closed {tf} candles")
    r.ok_gate("data", f"{tf} data is fresh, aligned and free of recent defects.")
    a = run_analyses(series, cfg, symbol, r.tfs)[tf]
    analyses = {tf: a}
    if not a.analyzed:
        return r.wait("analysis", f"{tf} could not be analysed ({a.data_status}: {a.data_reason or a.bias_basis}).", analyses=analyses)
    highs, lows = confirmed_swings(a, "high"), confirmed_swings(a, "low")
    cur = rows[-1]
    t_now = cur[0]
    cands = []
    if len(highs) >= 2 and highs[-1]["price"] < highs[-2]["price"]:
        cands.append((BEAR, highs[-2], highs[-1], lows))
    if len(lows) >= 2 and lows[-1]["price"] > lows[-2]["price"]:
        cands.append((BULL, lows[-2], lows[-1], highs))
    r.extra["trendlines"] = []
    if not cands:
        r.ctx.update(label="No trendline", summary="No pair of descending confirmed swing highs or ascending confirmed swing lows to draw a trendline through.")
        return r.wait("trendline", r.ctx["summary"], "Two descending swing highs or two ascending swing lows", analyses=analyses)
    problems = []
    for d, p1, p2, opposite in cands:
        value = line_at(p1, p2, t_now)
        between = [o for o in opposite if p1["time"] <= o["time"] <= p2["time"]]
        extreme = (min(between, key=lambda o: o["price"]) if d == BEAR else max(between, key=lambda o: o["price"])) if between else None
        info = {"direction": d, "kind": "resistance" if d == BEAR else "support", "t1": p1["time"], "p1": p1["price"], "t2": p2["time"], "p2": p2["price"], "value_now": value, "intact": None, "target_level": None}
        r.extra["trendlines"].append(info)
        if value is None:
            problems.append("the anchors share a time"); continue
        after = [x for x in rows if x[0] > p2["time"]]
        broken = any((x[4] > line_at(p1, p2, x[0])) if d == BEAR else (x[4] < line_at(p1, p2, x[0])) for x in after)
        info["intact"] = not broken
        if broken:
            problems.append(f"the {info['kind']} line has been closed through since its second anchor"); continue
        close = cur[4]
        respecting = close < value if d == BEAR else close > value
        if not respecting:
            problems.append(f"the newest close {close:g} is not {'below' if d == BEAR else 'above'} the {info['kind']} line ({value:g})"); continue
        if extreme is None:
            problems.append("there is no opposite swing between the anchors to indicate a target level"); continue
        # the parallel line through the extreme opposite swing, projected to the signal candle
        offset = extreme["price"] - line_at(p1, p2, extreme["time"])
        tgt = value + offset
        info["target_level"] = tgt
        entry = close
        sl = p2["price"] + params.sl_buffer if d == BEAR else p2["price"] - params.sl_buffer
        if (d == BEAR and not tgt < entry) or (d == BULL and not tgt > entry):
            problems.append(f"the indicated target level {tgt:g} is not beyond the entry {entry:g}"); continue
        risk, reward = abs(entry - sl), abs(tgt - entry)
        if risk <= 0 or reward / risk < params.min_rr:
            problems.append(f"reward-to-risk {reward / risk if risk else 0:.2f} is below the required {params.min_rr:g}"); continue
        word = "BUY" if d == BULL else "SELL"
        names = {BEAR: "descending (lower highs) resistance", BULL: "ascending (higher lows) support"}
        r.ctx.update(label=f"{names[d].capitalize()} line", direction=d, summary=f"{word}: the close {entry:g} respects the {names[d]} line ({value:g}).",
                     lines=[f"Line through {p1['price']:g} -> {p2['price']:g}, now at {value:g}", f"Target level {tgt:g}"])
        r.ok_gate("trendline", r.ctx["summary"])
        why = f"{word} on the close of the newest {tf} candle: it respects the {names[d]} line; target the indicated level {tgt:g}, stop beyond the second anchor {p2['price']:g}."
        return r.signal(d, entry, sl, tgt, why, f"{tf}:{int(p1['time'])}:{int(p2['time'])}", "The parallel line through the extreme opposite swing, projected to the signal candle.",
                        f"the indicated level {tgt:g} (parallel line through the {extreme['price']:g} swing)", f"beyond the second anchor swing {p2['price']:g}", analyses=analyses)
    r.ctx.update(label="Trendline not tradable", summary="; ".join(problems).capitalize() + ".")
    return r.wait("trendline", r.ctx["summary"], analyses=analyses)
