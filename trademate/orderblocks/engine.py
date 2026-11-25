"""Order Block (OB) detection engine (Phase 7C, task 3). Objective, deterministic, analysis only.

It never trades, sizes a position, sets a stop or target, or influences the Risk Engine, and it is not connected to the chart, the
Analysis page or any strategy yet.

WHAT AN ORDER BLOCK IS HERE (and is not)
  An order block is a CANDLE ZONE that price structure singles out as the base of a move that changed or continued market structure.
  TradeMate has candles and nothing else: no volume, no order book, no order flow, no knowledge of who traded. So nothing here
  detects institutional activity, resting orders or intent, and a detected block does not say price will return to it, react to it
  or reverse. "Order block" is a name for a price-action pattern, and a confirmed block means only that the structural conditions
  below were met. It carries no quality score and is not a trade setup.

INPUT
  The candles AND the Phase 7A Market Structure result computed on the SAME candles (as the Liquidity engine does): confirmed swing
  points (each knowable only from its confirmation candle) and BOS / CHoCH break events. The structure result decides what counts
  as a swing and as a break; this engine adds nothing to those definitions.

THE RULE (bullish; the bearish rule is the exact mirror: swap high/low, up/down, bearish/bullish candle)
  Terms: a bearish candle has close < open, a bullish candle close > open; a doji (close == open) is neither.
  1. CANDIDATE. For every confirmed swing LOW L (pivot candle o) that has an earlier confirmed swing HIGH, take H = the most recent
     swing high before o (the high the down-leg started from). The SOURCE candle is the last BEARISH candle at or before o and
     after H's pivot candle (searching backwards from o). No such candle, or no earlier swing high: no block. Several swing lows
     that map to the same source candle describe one block. Zone: upper = source high, lower = source low (the full candle
     range; the open and close are also reported so a consumer can use the body instead). A candidate is only an observation:
     "the last down candle before a swing low", and it is NOT confirmed. It is known from the candle at which L became knowable.
  2. CONFIRMED. The structure engine reports a bullish break event E (BOS or CHoCH) of exactly that swing high H, on a candle
     after o, and:
       * E is not a same-candle two-sided break (the order inside such a candle is unknowable);
       * the leg's ORIGIN is L: o is the lowest low (the EARLIEST one if equal, which is also the structure engine's own pivot
         tie-break) between H's pivot and the candle before E, i.e. the block belongs to the deepest point of the move
         that broke structure, not to an intermediate pullback;
       * (optional) max_leg_candles: E happened no more than that many candles after the source candle. Off by default;
         without it a slow grind that finally breaks the high also confirms, and this engine cannot tell an impulsive break
         from a slow one (see limitations).
     confirmed_time = the later of E's candle and the candle at which L became knowable. Nothing later is ever used.
  3. INVALIDATED (price only). close mode (default): a candle CLOSES below the zone's lower boundary; wick mode: a candle trades
     below it. Checked on every candle after the source candle. A block invalidated at or before the candle at which it would
     have been confirmed is never confirmed (price is checked FIRST on a candle: on a candle that both breaks structure and
     violates the zone, the conservative reading wins). A confirmed block that is violated later is reported "invalidated" with its
     confirmed_time kept, so the history is visible. Invalidation by a candle BEFORE the block was identified is possible
     (invalidated_time earlier than identified_time): the block was then never a live candidate.
  4. EXPIRED (structural bookkeeping, not price). A candidate that can no longer be confirmed: its swing high H was REPLACED by a
     newer confirmed swing high without being broken, H was broken ambiguously (same candle, both sides), or H was broken
     but not by this block's leg (a different origin, or beyond max_leg_candles). Codes: REFERENCE_LOST, NOT_QUALIFIED.
     If H was already broken before the swing low, no block is created at all (there is nothing left for it to confirm).
  Statuses: candidate | confirmed | invalidated | expired. invalidated and expired are final.

NO LOOK-AHEAD
  Every state change carries the time of the candle that established it, and only candles up to that candle are used to establish
  it. Analysing a prefix of the data gives, for every block it reports, the same identified / confirmed / invalidated / expired
  times as the full data (a prefix may show a block as a candidate that the full data later confirms, never the reverse).

FORMING CANDLE
  The newest candle is treated as still forming exactly when the structure result says so (`structure.config.last_candle_closed` is
  False; there is one source of truth). A block whose CURRENT status was established by that candle (identified, confirmed,
  invalidated or expired on it) has provisional=True: it may change or vanish when the candle closes. Require provisional == False
  for settled evidence.

DATA TREATMENT (fail closed, as the FVG engine: an OB depends on which candles are neighbours)
  Cleaning is the structure engine's (invalid excluded, duplicate times keep the last, out-of-order sorted). Beyond that an invalid
  candle, a price <= 0, or two rows with the same time but different prices gives data_status="invalid" and no blocks. Too few
  candles for the structure analysis gives "insufficient". A structure result that does not match these candles (different
  count, times or prices) gives "inconsistent" and no blocks. Missing values are never filled in.

IDENTITY: id = `OB-<BULL|BEAR>-<source candle time in whole epoch seconds>`. Pure function of the inputs: the same data analysed
  twice, with repeated rows, or as a growing series yields each block once with the same id.
  `swing_id` and `confirming_event_id` echo the structure engine's own ids. Those are formatted with %g (6 significant digits), so with
  real epoch timestamps different swings / events can share an id. This engine never relies on them: to find the swing or the
  event use `origin_time`, `reference_time` and `confirming_event_time`.

SYMBOL / TIMEFRAME: optional labels copied onto the result and each block; they never affect detection.

NOT IMPLEMENTED (no justified specification): volume, displacement / candle-size or volatility thresholds, imbalance (FVG)
  requirement, mitigation / retest tracking, body-only zones, higher-timeframe logic, breaker blocks, ranking or scoring,
  chart / Analysis integration.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from trademate.structure import Diagnostics, StructureResult, clean_candles

BULLISH, BEARISH = "bullish", "bearish"
CANDIDATE, CONFIRMED, INVALIDATED, EXPIRED = "candidate", "confirmed", "invalidated", "expired"
REFERENCE_LOST, NOT_QUALIFIED = "REFERENCE_LOST", "NOT_QUALIFIED"
INVALIDATION_MODES = ("close", "wick")
DATA_OK, DATA_INSUFFICIENT, DATA_INVALID, DATA_INCONSISTENT = "ok", "insufficient", "invalid", "inconsistent"
RULE_ID = "structure-leg-origin-v1"


@dataclass(frozen=True)
class OrderBlockConfig:
    invalidation_mode: str = "close"        # "close": a close beyond the far boundary invalidates; "wick": any trade beyond it
    max_leg_candles: int | None = None      # optional: a break more than this many candles after the source candle does not confirm

    def validate(self) -> "OrderBlockConfig":
        if self.invalidation_mode not in INVALIDATION_MODES:
            raise ValueError(f"invalidation_mode must be one of: {', '.join(INVALIDATION_MODES)}.")
        m = self.max_leg_candles
        if m is not None and (isinstance(m, bool) or not isinstance(m, int) or m < 1):
            raise ValueError("max_leg_candles must be a whole number of 1 or more, or None.")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OrderBlock:
    id: str
    direction: str                      # "bullish" (demand zone, last bearish candle) | "bearish" (supply zone, last bullish candle)
    status: str                         # candidate | confirmed | invalidated | expired
    upper: float                        # source candle high
    lower: float                        # source candle low (lower < upper)
    size: float
    source_time: float                  # the source candle
    source_index: int                   # position in the CLEANED, time-sorted candle list (not in the caller's input)
    source_open: float
    source_close: float
    origin_time: float                  # pivot candle of the swing extreme this block belongs to (the leg's origin when confirmed)
    swing_id: str                       # that swing's id in the structure result
    reference_time: float               # pivot candle of the swing whose break would confirm it (or did)
    reference_level: float              # that swing's price
    identified_time: float              # candle at which the swing became knowable, i.e. when this candidate exists
    confirmed_time: float | None        # None unless it was structurally confirmed (kept if invalidated later)
    invalidated_time: float | None
    expired_time: float | None
    expired_reason: str | None          # REFERENCE_LOST | NOT_QUALIFIED
    confirming_event_id: str | None     # the structure event that confirmed it
    confirming_event_type: str | None   # BOS | CHOCH
    confirming_event_time: float | None  # the candle that broke the reference level (use this, not the id, to find the event)
    provisional: bool                   # the candle that set the current status is the still-forming candle
    rule: str                           # which rule version produced it
    explanation: str                    # one sentence: what qualified it and what it is not
    symbol: str | None = None
    timeframe: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OrderBlockResult:
    config: OrderBlockConfig
    blocks: tuple = ()                  # oldest source candle first, every block exactly once
    data_status: str = DATA_OK          # ok | insufficient | invalid | inconsistent
    reason: str = ""
    candles_used: int = 0
    revision: str = ""
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    non_positive_price_times: tuple = ()
    symbol: str | None = None
    timeframe: str | None = None
    note: str = ("An order block is a candle zone selected by price structure alone (TradeMate has no volume or order-flow data). "
                 "It is an observation, not evidence of institutional activity, a prediction, a quality rating or a trade signal.")

    def to_dict(self) -> dict:
        return {"config": self.config.to_dict(), "blocks": [b.to_dict() for b in self.blocks], "data_status": self.data_status,
                "reason": self.reason, "candles_used": self.candles_used, "revision": self.revision,
                "diagnostics": self.diagnostics.to_dict(), "non_positive_price_times": list(self.non_positive_price_times),
                "symbol": self.symbol, "timeframe": self.timeframe, "note": self.note}


def _tid(value: float) -> str:
    """A time as plain digits for ids (epoch seconds: 1760000000, never 1.76e+09)."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _p(value: float) -> str:
    return repr(float(value))


def _revision(bars) -> str:
    h = hashlib.sha1()
    for b in bars:
        h.update(f"{b.time!r}|{b.open!r}|{b.high!r}|{b.low!r}|{b.close!r};".encode())
    return h.hexdigest()[:12]


def _label(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text or None.")
    return value


def _inconsistency(bars, structure: StructureResult) -> str | None:
    """Why `structure` was not computed on these candles, or None. The indices in a structure result mean positions in the cleaned
    candle list, so a result from different candles would silently point at the wrong candles."""
    n = len(bars)
    if structure.diagnostics.used_count != n:
        return f"The structure result used {structure.diagnostics.used_count} candles but {n} are being analysed."
    for s in structure.swings:
        if s.kind not in ("high", "low") or not (0 <= s.pivot_index < n and 0 <= s.confirmed_index < n):
            return f"Swing {s.id} points outside the candles."
        b = bars[s.pivot_index]
        if b.time != s.pivot_time or bars[s.confirmed_index].time != s.confirmed_time or s.price != (b.high if s.kind == "high" else b.low):
            return f"Swing {s.id} does not match the candles."
    for e in structure.events:
        if not 0 <= e.candle_index < n or bars[e.candle_index].time != e.candle_time:
            return f"Event {e.id} does not match the candles."
    return None


def analyze_order_blocks(candles: Iterable[Any], structure: StructureResult, config: OrderBlockConfig | None = None, *,
                         symbol: str | None = None, timeframe: str | None = None) -> OrderBlockResult:
    """Order blocks in `candles`, using the structure result computed on the SAME candles (see the module docstring for every rule).
    Raises ValueError for a bad config or a non-text symbol / timeframe; bad CANDLES never raise: they are reported in `data_status` /
    `diagnostics` and no block is returned."""
    cfg = (config or OrderBlockConfig()).validate()
    symbol, timeframe = _label(symbol, "symbol"), _label(timeframe, "timeframe")
    if not isinstance(structure, StructureResult):
        raise ValueError("structure must be a StructureResult computed on the same candles.")
    bars, diag = clean_candles(candles)
    n = len(bars)
    base = dict(config=cfg, candles_used=n, diagnostics=diag, symbol=symbol, timeframe=timeframe)

    bad_prices = tuple(b.time for b in bars if min(b.open, b.high, b.low, b.close) <= 0)
    if diag.invalid or diag.duplicates_conflicting or bad_prices:
        parts = []
        if diag.invalid:
            parts.append(f"{len(diag.invalid)} candle(s) had impossible or missing values")
        if bad_prices:
            parts.append(f"{len(bad_prices)} candle(s) had a price of zero or below")
        if diag.duplicates_conflicting:
            parts.append(f"{diag.duplicates_conflicting} duplicate timestamp(s) had conflicting prices")
        return OrderBlockResult(**base, data_status=DATA_INVALID, non_positive_price_times=bad_prices,
                                reason="; ".join(parts) + ". No blocks were reported: excluding such candles would make their "
                                                          "neighbours look adjacent and could create a block that never existed.")
    if n < structure.config.min_candles or structure.diagnostics.insufficient_data:
        return OrderBlockResult(**base, data_status=DATA_INSUFFICIENT, revision=_revision(bars),
                                reason=f"Need at least {structure.config.min_candles} usable candles for the structure analysis, got {n}.")
    why = _inconsistency(bars, structure)
    if why:
        return OrderBlockResult(**base, data_status=DATA_INCONSISTENT, revision=_revision(bars),
                                reason=why + " The structure result must be computed on exactly the candles given here.")

    forming = None if structure.config.last_candle_closed else n - 1
    R = structure.config.swing_right
    swings = sorted(structure.swings, key=lambda s: (s.pivot_index, s.kind))
    blocks: list[OrderBlock] = []
    for direction in (BULLISH, BEARISH):
        blocks.extend(_side(direction, bars, n, swings, structure.events, cfg, forming, R, symbol, timeframe))
    blocks.sort(key=lambda b: (b.source_time, b.direction))
    return OrderBlockResult(**base, blocks=tuple(blocks), revision=_revision(bars))


def _side(direction, bars, n, swings, events, cfg, forming, R, symbol, timeframe) -> list:
    bull = direction == BULLISH
    ref_kind, origin_kind = ("high", "low") if bull else ("low", "high")
    refs = [s for s in swings if s.kind == ref_kind]
    ref_pivots = [s.pivot_index for s in refs]
    origins = [s for s in swings if s.kind == origin_kind]
    event_for = {e.reference_pivot_time: e for e in events if e.direction == direction}
    source_colour = (lambda b: b.close < b.open) if bull else (lambda b: b.close > b.open)

    # ---- 1. candidates: one per source candle ----
    records: dict[int, dict] = {}
    for o in origins:
        k = bisect_left(ref_pivots, o.pivot_index)            # refs strictly before the origin pivot
        if k == 0:
            continue                                          # no earlier opposite swing: the leg has no known start
        ref = refs[k - 1]
        ev = event_for.get(ref.pivot_time)
        # A reference swing stops being eligible when a newer same-kind swing is
        # confirmed before its break is confirmed. Do not use a later break event
        # to resurrect a candidate that was already structurally expired in an
        # earlier candle prefix (this also preserves prefix/look-ahead consistency).
        newer_ref = next((candidate for candidate in refs if candidate.pivot_index > ref.pivot_index), None)
        if ev is not None and newer_ref is not None and newer_ref.confirmed_index < ev.candle_index:
            ev = None
        if ev is not None and ev.candle_index <= o.pivot_index:
            continue                                          # the reference was already broken before this swing: nothing left to confirm
        source = next((i for i in range(o.pivot_index, ref.pivot_index, -1) if source_colour(bars[i])), None)
        if source is None:
            continue
        rec = records.get(source)
        if rec is None:
            records[source] = {"source": source, "ref": ref, "event": ev, "origins": [o]}
        elif rec["ref"] is ref:
            rec["origins"].append(o)

    # ---- 2. lifecycle of each candidate ----
    out = []
    for source in sorted(records):
        rec = records[source]
        ref, ev, org = rec["ref"], rec["event"], rec["origins"]
        src = bars[source]
        upper, lower = src.high, src.low
        ident = min(o.confirmed_index for o in org)

        violated = None                                       # first candle after the source that breaks the zone
        for i in range(source + 1, n):
            b = bars[i]
            if bull and (b.low if cfg.invalidation_mode == "wick" else b.close) < lower:
                violated = i
                break
            if not bull and (b.high if cfg.invalidation_mode == "wick" else b.close) > upper:
                violated = i
                break

        end_kind = end_idx = reason = detail = None           # confirmation (ci) or expiry; None = still a candidate
        matched = None                                        # the swing that IS the leg's origin (set when the break qualifies)
        if ev is None:
            newer = next((s for s in refs if s.pivot_index > ref.pivot_index), None)
            if newer is not None:
                end_kind, end_idx, reason = "expire", newer.confirmed_index, REFERENCE_LOST
                detail = f"the swing {ref_kind} at {_p(ref.price)} was replaced by a newer swing {ref_kind} without being broken"
        else:
            e = ev.candle_index
            if ev.same_candle_conflict:
                end_kind, end_idx, reason = "expire", max(e, ident), REFERENCE_LOST
                detail = f"the swing {ref_kind} at {_p(ref.price)} was broken in a candle that also broke the other side (order unknowable)"
            elif cfg.max_leg_candles is not None and e - source > cfg.max_leg_candles:
                end_kind, end_idx, reason = "expire", max(e, ident), NOT_QUALIFIED
                detail = f"the break came {e - source} candles after the source candle (limit {cfg.max_leg_candles})"
            else:
                window = range(ref.pivot_index + 1, e)
                if bull:                                       # the leg's origin: the EARLIEST lowest low (highest high) before the break
                    star = min(window, key=lambda i: (bars[i].low, i))
                else:
                    star = max(window, key=lambda i: (bars[i].high, -i))
                known_at = max(e, star + R)                    # the earliest candle at which we can know whether `star` is a swing
                if known_at <= n - 1:
                    match = next((o for o in org if o.pivot_index == star), None)
                    if match is not None:
                        end_kind, end_idx, matched = "confirm", max(e, match.confirmed_index), match
                    else:
                        end_kind, end_idx, reason = "expire", known_at, NOT_QUALIFIED
                        detail = (f"the {ref_kind} at {_p(ref.price)} was broken, but the leg's origin was a different candle "
                                  f"(time {_tid(bars[star].time)}), not this block's swing")

        status, confirmed_idx, invalid_idx, expired_idx = CANDIDATE, None, None, None
        if violated is not None and (end_idx is None or violated <= end_idx):
            status, invalid_idx = INVALIDATED, violated        # price is checked first; never confirmed
        elif end_kind == "confirm":
            confirmed_idx = end_idx
            status = CONFIRMED
            if violated is not None:                           # violated after confirmation (violated > end_idx)
                status, invalid_idx = INVALIDATED, violated
        elif end_kind == "expire":
            status, expired_idx = EXPIRED, end_idx

        origin = matched if confirmed_idx is not None else org[0]      # a confirmed block reports the swing that was the leg's origin
        established = {CANDIDATE: ident, CONFIRMED: confirmed_idx, INVALIDATED: invalid_idx, EXPIRED: expired_idx}[status]
        t = lambda i: None if i is None else bars[i].time      # noqa: E731
        colour = "bearish" if bull else "bullish"
        side_word = "swing low" if bull else "swing high"
        base_text = (f"Last {colour} candle (time {_tid(src.time)}) at or before the {side_word} at {_tid(bars[origin.pivot_index].time)}, "
                     f"after the {ref_kind} at {_p(ref.price)}")
        broke = "above" if bull else "below"
        if status == CANDIDATE:
            text = f"Candidate: {base_text}. Not confirmed: that level has not been broken {broke} by a {direction} structure break from this leg."
        elif status == CONFIRMED:
            text = (f"Confirmed: {base_text}, the origin of the leg whose {direction} {ev.type} broke {ref_kind} {_p(ref.price)} "
                    f"(candle time {_tid(ev.candle_time)}).")
        elif status == INVALIDATED:
            beyond = "below" if bull else "above"
            how = "traded" if cfg.invalidation_mode == "wick" else "closed"
            text = (f"Invalidated: price {how} {beyond} the zone's {'lower' if bull else 'upper'} boundary {_p(lower if bull else upper)} "
                    f"at candle time {_tid(bars[invalid_idx].time)}"
                    + (f", after it was confirmed at {_tid(bars[confirmed_idx].time)}." if confirmed_idx is not None
                       else "; it was never confirmed."))
        else:
            text = f"Expired: {detail}, so this candidate can no longer be confirmed. {base_text}."
        out.append(OrderBlock(
            id=f"OB-{'BULL' if bull else 'BEAR'}-{_tid(src.time)}", direction=direction, status=status, upper=upper, lower=lower,
            size=upper - lower, source_time=src.time, source_index=source, source_open=src.open, source_close=src.close,
            origin_time=bars[origin.pivot_index].time, swing_id=origin.id, reference_time=ref.pivot_time, reference_level=ref.price,
            identified_time=bars[ident].time, confirmed_time=t(confirmed_idx), invalidated_time=t(invalid_idx),
            expired_time=t(expired_idx), expired_reason=reason if status == EXPIRED else None,
            confirming_event_id=ev.id if confirmed_idx is not None else None,
            confirming_event_type=ev.type if confirmed_idx is not None else None,
            confirming_event_time=ev.candle_time if confirmed_idx is not None else None,
            provisional=forming is not None and established == forming, rule=RULE_ID, explanation=text,
            symbol=symbol, timeframe=timeframe))
    return out
