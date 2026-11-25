"""Strategy: Market Structure Trading (id "market_structure").  RULE "MARKET-STRUCTURE-v1".   One timeframe: the user's execution timeframe.

Direction comes ONLY from the confirmed swing structure of the chosen timeframe (the shared structure engine; closed candles, confirmed swings):
  Bullish   the newest confirmed swing high is a Higher High AND the newest confirmed swing low is a Higher Low.
  Bearish   the newest confirmed swing high is a Lower High  AND the newest confirmed swing low is a Lower Low.
  Anything else (HH with LL, equal highs, too few swings ...) is contradictory or unproven: WAIT. Direction is never invented.
A straight line is drawn through the two most recent confirmed swing highs (HH in an uptrend, LH in a downtrend) to show the movement (result["structure_lines"]).
Entry     as soon as the structure is established: no extra confirmation candle, no other indicator. Reference = close of the newest closed candle.
Fresh     the newest confirmed swing was confirmed at most `signal_max_age` closed candles ago; an older structure signal has expired (WAIT).
Once      the setup identity is the structure state (direction + newest swing high and low), so the same structure is never entered twice.
Stop      beyond the newest protected swing (BUY: below the Higher Low; SELL: above the Lower High) plus the configured buffer.
Risk/reward  no minimum is enforced: the target and the stop are the user's own choice (the calculator shows the resulting ratio), so a legitimate entry is never refused by an extra rule.
Target    a fixed, configurable distance in pips from the actual entry (params.target_pips; settings [strategy] market_structure_target_pips), OR, when the user has chosen a
          risk-to-reward ratio (saved setting `risk_reward`, 1:N), N x the FINAL stop distance. With no ratio chosen the behaviour is exactly as before.
This strategy does not read CHoCH, Trendline or Candle Momentum.
"""

from __future__ import annotations

from trademate.strategies._single_tf import Run, closed_rows_problem
from trademate.strategies.base import BEAR, BULL, confirmed_swings, finite, run_analyses


def _line(a, b):
    return {"kind": "swing_highs", "t1": a["time"], "p1": a["price"], "t2": b["time"], "p2": b["price"]}


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
    an = run_analyses(series, cfg, symbol, r.tfs)
    a = an[tf]
    analyses = {tf: a}
    if not a.analyzed:
        return r.wait("analysis", f"{tf} could not be analysed ({a.data_status}: {a.data_reason or a.bias_basis}).", analyses=analyses)
    highs, lows = confirmed_swings(a, "high"), confirmed_swings(a, "low")
    if len(highs) < 2 or len(lows) < 2:
        r.ctx.update(label="Structure not established", summary=f"Only {len(highs)} confirmed swing high(s) and {len(lows)} swing low(s): at least two of each are needed.")
        return r.wait("swings", r.ctx["summary"], "Two confirmed swing highs and two confirmed swing lows", analyses=analyses)
    hi, lo = highs[-1], lows[-1]
    hc, lc = hi.get("classification"), lo.get("classification")
    r.ok_gate("swings", f"Newest confirmed swing high is {hc or 'unclassified'} ({hi['price']:g}); newest confirmed swing low is {lc or 'unclassified'} ({lo['price']:g}).")
    if hc == "HH" and lc == "HL":
        d, label = BULL, "Bullish structure (HH + HL)"
    elif hc == "LH" and lc == "LL":
        d, label = BEAR, "Bearish structure (LH + LL)"
    else:
        r.ctx.update(label="Structure contradictory or unproven", summary=f"Newest swing high is {hc or 'unclassified'} and newest swing low is {lc or 'unclassified'}: the evidence does not establish a direction.")
        return r.wait("direction", r.ctx["summary"], analyses=analyses)
    prev_high = highs[-2]
    line = _line(prev_high, hi)
    r.extra["structure_lines"] = [line]
    r.ctx.update(label=label, direction=d, summary=f"{label}: highs {prev_high['price']:g} -> {hi['price']:g}, lows {lows[-2]['price']:g} -> {lo['price']:g}.",
                 lines=[f"Line across the last two swing highs: {prev_high['price']:g} -> {hi['price']:g}"])
    r.ok_gate("direction", r.ctx["summary"])
    n = len(rows)
    newest_swing_confirmed = max(hi.get("confirmed_time") or 0, lo.get("confirmed_time") or 0)
    age = sum(1 for row in rows if row[0] >= newest_swing_confirmed) if newest_swing_confirmed else n
    if age > params.signal_max_age:
        return r.wait("fresh", f"The structure was established {age} closed candles ago (limit {params.signal_max_age}); the signal has expired.", context_met=True, analyses=analyses)
    r.ok_gate("fresh", f"The newest confirmed swing is {age} closed candle(s) old (limit {params.signal_max_age}).")
    entry = rows[-1][4]
    protected = lo["price"] if d == BULL else hi["price"]
    sl = protected - params.sl_buffer if d == BULL else protected + params.sl_buffer
    rr = params.risk_reward
    if rr:
        tp, info = None, None                                  # the ratio is applied to the FINAL stop distance inside Run.signal
    else:
        tp, info = r.fixed_target(d, entry)
        if tp is None:
            return r.wait("target", f"No target can be set: {info}.", "A configured target distance and a known pip size", context_met=True, analyses=analyses)
    word = "BUY" if d == BULL else "SELL"
    goal = f"target 1:{rr:g} risk-to-reward" if rr else f"target {params.target_pips:g} pips from the entry"
    why = f"{label} is established; {word} on the close of the newest {tf} candle. Stop beyond the protected swing {protected:g}, {goal}."
    anchor = f"{tf}:{int(hi['time'])}:{int(lo['time'])}"
    return r.signal(d, entry, sl, tp, why, anchor, f"Fixed {params.target_pips:g} pips from the entry reference.", f"fixed {params.target_pips:g} pips from the entry (the real target is set from the executable entry)",
                    f"beyond the protected swing {protected:g}" + (f" plus buffer {params.sl_buffer:g}" if params.sl_buffer else ""), target_info=info, analyses=analyses, min_rr=0.0, rr=rr)
