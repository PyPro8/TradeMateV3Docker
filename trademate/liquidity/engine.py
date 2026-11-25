"""Liquidity Mechanics Engine (Phase 7B). Objective, deterministic, analysis only.

WHAT A "POOL" IS (and is not)
  A pool is a price LEVEL where an observable swing high (or several nearly equal ones) sits. Many traders place stop orders
  just beyond obvious highs and lows, so such a level is a POTENTIAL liquidity area, inferred from price structure alone.
  TradeMate cannot see anyone's orders: a pool does not reveal hidden orders, and nothing here says price WILL go there.
    BSL (buy-side liquidity)  : above a swing high / equal highs  (where buy stops and breakout buys may rest)
    SSL (sell-side liquidity) : below a swing low  / equal lows   (where sell stops and breakdown sells may rest)

INPUT: the confirmed swings of the Phase 7A Market Structure Engine (so a swing is only used from the candle at which it became
knowable) and the same cleaned candles. Nothing from a later candle is ever used to describe an earlier moment.

POOL RULES
  * Candidate: every confirmed swing high (BSL) / swing low (SSL) not yet swept.
  * Clustering ("equal highs/lows"): swings are processed in the order they became KNOWN (confirmation candle, then pivot
    order). A new swing joins the nearest ACTIVE same-side pool when |price - pool level| <= tolerance (ties: the older pool);
    otherwise it starts a new pool. Pools are never merged afterwards. Tolerance = tolerance_ticks x the symbol's tick size,
    set by the caller from the broker's own symbol data (never a hard-coded price).
  * Representative price (`level`): BSL = the HIGHEST member price; SSL = the LOWEST. That is the first price a stop beyond
    the cluster would be at, and it is the price a sweep must cross.
  * kind: "swing" (one member) or "equal" (two or more members within tolerance).
  * Timestamps: `created_time` = pivot candle of the FIRST member (when the extreme occurred); `confirmed_time` = candle at
    which that first member became knowable (the pool is a known fact from then on); `equal_confirmed_time` = candle at which
    the second member became knowable (an "equal" pool is only known to be equal from then on).
  * Stable id: `<side>-<first member's pivot time in whole epoch seconds>` (e.g. BSL-1760000000). Joining members or sweeping never changes it;
    re-running the analysis gives the same ids and the same pools (no duplicates).
  * States: ACTIVE -> SWEPT (a sweep rule matched) or ACTIVE -> EXPIRED (older than max_age_candles). SWEPT and EXPIRED are
    final. `provisional` is True while the only evidence is the still-forming candle.

SWEEP RULES (checked candle by candle, BEFORE that candle's newly known swings are added, exactly as the structure engine
checks breaks before activating swings; a pool can only be swept by a candle AFTER the one that made it knowable)
  * wick mode : BSL swept by a candle whose HIGH is above level + min_penetration; SSL by a LOW below level - min_penetration.
  * close mode: the candle's CLOSE must be beyond level +/- min_penetration.
  * The price must go BEYOND the level by more than max(min_penetration, tolerance). Within the equal-tolerance a price is still
    "at the level" (it may be another equal high forming), so poking that little beyond is not a sweep. With both at 0, any price
    strictly beyond the level counts. min_penetration = sweep_min_ticks x tick size.
  * `close_beyond` records whether that same candle also CLOSED beyond the level. In wick mode close_beyond=False means price
    pushed through and closed back inside. This is an observed fact, not a reversal claim; a sweep never implies a reversal.
  * confirmed = the sweeping candle is closed; otherwise provisional (it can still vanish when the candle closes back inside).
  * Repeated sweeps: a pool is swept ONCE. Price returning to the level later does not create another event; a higher/lower
    swing creates a new pool with its own sweep.
  * Overlapping pools: several pools crossed by one candle each get their own sweep; they share `group` (all pool ids hit by
    that candle on that side). Pools are never merged to hide this.
  * Event id: `SWEEP-<pool id>-<candle time>`; one per pool, never duplicated.

STRUCTURE INTEGRATION (temporal facts only; never causation)
  For each sweep: events on the SAME candle ("order inside a candle cannot be known"), the latest event BEFORE it ("occurred
  earlier, so it cannot be a consequence of the sweep"), and the first event AFTER it within link_window candles. A sweep that
  is followed by no structural break is reported as such; a break is never forced onto a sweep.

DATA TREATMENT
  * Pure function of the candles: if MT5 corrects a historical candle, the next run recomputes everything from the corrected
    data. Ids derive from pivot times, so unaffected pools keep their ids; `revision` (a hash of the cleaned candles) changes,
    and `diff_liquidity(old, new)` lists pools/sweeps added, removed or changed.
  * Invalid candles, conflicting duplicates and insufficient history are handled by the structure engine's cleaning; the
    caller (analysis layer) refuses to analyse such data rather than guess.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

from trademate.structure import StructureConfig, StructureResult, analyze_cached, clean_candles

SWEEP_MODES = ("wick", "close")
ACTIVE, SWEPT, EXPIRED = "ACTIVE", "SWEPT", "EXPIRED"


@dataclass(frozen=True)
class LiquidityConfig:
    tolerance: float = 0.0              # absolute price distance within which swings are "equal" (ticks x tick size, set by the caller)
    sweep_mode: str = "wick"            # "wick" | "close"
    sweep_min_penetration: float = 0.0  # price beyond the level required (absolute); 0 = any amount strictly beyond
    max_pools_per_side: int = 8         # most recent pools shown per side (older ones are counted, not listed)
    max_age_candles: int = 500          # an unswept pool older than this many candles becomes EXPIRED
    link_window: int = 10               # candles after a sweep in which a structural break is reported as "followed"
    last_candle_closed: bool = False    # False (default) = the newest candle is still forming

    def validate(self) -> "LiquidityConfig":
        for name in ("tolerance", "sweep_min_penetration"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} must be a finite number >= 0.")
        if self.sweep_mode not in SWEEP_MODES:
            raise ValueError(f"sweep_mode must be one of: {', '.join(SWEEP_MODES)}.")
        for name, lo, hi in (("max_pools_per_side", 1, 50), ("max_age_candles", 10, 5000), ("link_window", 1, 100)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{name} must be a whole number from {lo} to {hi}.")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Pool:
    id: str
    side: str                       # "BSL" | "SSL"
    kind: str                       # "swing" | "equal"
    level: float                    # representative price (BSL: highest member; SSL: lowest member)
    members: list                   # [{id, price, pivot_time, confirmed_time}] in the order they became known
    created_time: float
    confirmed_time: float
    equal_confirmed_time: float | None
    state: str = ACTIVE
    provisional: bool = False
    swept_time: float | None = None
    expired_time: float | None = None
    created_index: int = 0
    confirmed_index: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["member_count"] = len(self.members)
        d["tolerance_spread"] = (max(m["price"] for m in self.members) - min(m["price"] for m in self.members))
        return d


@dataclass(frozen=True)
class Sweep:
    id: str
    pool_id: str
    side: str
    pool_kind: str
    member_count: int
    level: float
    candle_time: float
    high: float
    low: float
    close: float
    penetration: float              # how far beyond the level the candle's high (BSL) / low (SSL) went
    mode: str
    close_beyond: bool              # the same candle also CLOSED beyond the level
    confirmed: bool
    group: tuple                    # ids of every pool this candle crossed on this side (includes this one)
    structure: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["group"] = list(self.group)
        return d


@dataclass(frozen=True)
class LiquidityResult:
    config: LiquidityConfig
    pools: tuple
    sweeps: tuple
    revision: str
    candles_used: int
    hidden_pools: dict
    note: str = "Potential liquidity areas inferred from price structure. They are not a view of actual orders."

    def to_dict(self) -> dict:
        return {"config": self.config.to_dict(), "pools": [p.to_dict() for p in self.pools],
                "sweeps": [s.to_dict() for s in self.sweeps], "revision": self.revision, "candles_used": self.candles_used,
                "hidden_pools": self.hidden_pools, "note": self.note}


def ticks_to_price(ticks: float, tick_size: float) -> float:
    """ticks x tick size without float drift (5 ticks of 0.00001 is exactly 0.00005). Raises ValueError for unusable input."""
    from decimal import Decimal
    for name, v in (("ticks", ticks), ("tick_size", tick_size)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} must be a finite number >= 0.")
    if tick_size <= 0 and ticks > 0:
        raise ValueError("The symbol's tick size is not known, so a tolerance in ticks cannot be converted to a price.")
    return float(Decimal(str(ticks)) * Decimal(str(tick_size)))


def _tid(value: float) -> str:
    """A time as plain digits for ids (epoch seconds: 1760000000, never 1.76e+09, which would make different times collide)."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _revision(bars) -> str:
    h = hashlib.sha1()
    for b in bars:
        h.update(f"{b.time!r}|{b.open!r}|{b.high!r}|{b.low!r}|{b.close!r};".encode())
    return h.hexdigest()[:12]


def _member(s) -> dict:
    return {"id": s.id, "price": s.price, "pivot_time": s.pivot_time, "confirmed_time": s.confirmed_time}


def _structure_context(sweep_time: float, sweep_index: int, events, index_of: dict, window: int) -> dict:
    """Temporal facts about BOS/CHoCH events around one sweep. Never claims a cause."""
    before = after = None
    same = []
    for e in events:
        if e.candle_time == sweep_time:
            same.append(e)
        elif e.candle_time < sweep_time:
            before = e
        elif after is None:
            gap = index_of.get(e.candle_time, 10 ** 9) - sweep_index
            if 0 < gap <= window:
                after = e
    ev = lambda e: None if e is None else {"id": e.id, "type": e.type, "direction": e.direction, "level": e.level,
                                           "candle_time": e.candle_time, "confirmed": e.confirmed}
    lines = []
    if before is not None:
        lines.append(f"The latest structural event before this sweep was a {before.direction} {before.type} (earlier, so it is not a "
                     "consequence of the sweep and does not confirm it).")
    for e in same:
        lines.append(f"A {e.direction} {e.type} occurred on the same candle. The order of events inside one candle cannot be known, "
                     "so neither is claimed to follow the other.")
    if after is not None:
        lines.append(f"A {after.direction} {after.type} followed on a later candle"
                     + ("" if after.confirmed else " (still developing)") + ". This is a sequence in time, not proof of cause.")
    elif not same:
        lines.append(f"No structural break (BOS/CHoCH) followed within {window} candles"
                     + (" yet." if index_of and sweep_index >= max(index_of.values()) - window else "."))
    return {"before": ev(before), "same_candle": [ev(e) for e in same], "after": ev(after), "statements": lines}


def analyze_liquidity(candles: Iterable[Any], structure: StructureResult, cfg: LiquidityConfig | None = None) -> LiquidityResult:
    """Pools and sweeps from `candles` and the structure result computed on the SAME candles. Raises ValueError for bad config."""
    cfg = (cfg or LiquidityConfig()).validate()
    bars, _diag = clean_candles(candles)
    n = len(bars)
    forming = None if cfg.last_candle_closed or n == 0 else n - 1
    swings = sorted((s for s in structure.swings), key=lambda s: (s.confirmed_index, s.pivot_index, s.kind))
    by_confirm: dict[int, list] = {}
    for s in swings:
        by_confirm.setdefault(s.confirmed_index, []).append(s)

    pools: list[Pool] = []
    sweeps: list[Sweep] = []
    pen, tol = cfg.sweep_min_penetration, cfg.tolerance
    margin = max(pen, tol)      # price within the equal-tolerance of a level is still AT the level, never beyond it
    for k in range(n):
        bar = bars[k]
        # (a) sweeps and expiry of pools that were known BEFORE this candle
        hit: dict[str, list[Pool]] = {"BSL": [], "SSL": []}
        for p in pools:
            if p.state != ACTIVE or p.confirmed_index >= k:
                continue
            if k - p.created_index > cfg.max_age_candles:
                p.state, p.expired_time = EXPIRED, bar.time
                continue
            if p.side == "BSL":
                crossed = (bar.high if cfg.sweep_mode == "wick" else bar.close) > p.level + margin
            else:
                crossed = (bar.low if cfg.sweep_mode == "wick" else bar.close) < p.level - margin
            if crossed:
                hit[p.side].append(p)
        for side, group in hit.items():
            ids = tuple(p.id for p in sorted(group, key=lambda q: q.created_index))
            for p in sorted(group, key=lambda q: q.created_index):
                p.state, p.swept_time, p.provisional = SWEPT, bar.time, (k == forming)
                beyond = (bar.close > p.level) if side == "BSL" else (bar.close < p.level)
                excursion = (bar.high - p.level) if side == "BSL" else (p.level - bar.low)
                sweeps.append(Sweep(id=f"SWEEP-{p.id}-{_tid(bar.time)}", pool_id=p.id, side=side, pool_kind=p.kind, member_count=len(p.members),
                                    level=p.level, candle_time=bar.time, high=bar.high, low=bar.low, close=bar.close, penetration=excursion,
                                    mode=cfg.sweep_mode, close_beyond=beyond, confirmed=(k != forming), group=ids))
        # (b) swings that became knowable AT this candle join pools (after the sweep check, never before)
        for s in by_confirm.get(k, ()):
            side = "BSL" if s.kind == "high" else "SSL"
            near = [p for p in pools if p.side == side and p.state == ACTIVE and abs(s.price - p.level) <= tol]
            if near:
                p = min(near, key=lambda q: (abs(s.price - q.level), q.created_index))
                p.members.append(_member(s))
                p.level = max(p.level, s.price) if side == "BSL" else min(p.level, s.price)
                if p.kind == "swing":
                    p.kind, p.equal_confirmed_time = "equal", bars[k].time
                p.provisional = p.provisional or (k == forming)
            else:
                pools.append(Pool(id=f"{side}-{_tid(s.pivot_time)}", side=side, kind="swing", level=s.price, members=[_member(s)],
                                  created_time=s.pivot_time, confirmed_time=s.confirmed_time, equal_confirmed_time=None,
                                  provisional=(k == forming), created_index=s.pivot_index, confirmed_index=k))
    index_of = {b.time: i for i, b in enumerate(bars)}
    linked = []
    for sw in sweeps:
        ctx = _structure_context(sw.candle_time, index_of[sw.candle_time], structure.events, index_of, cfg.link_window)
        linked.append(replace(sw, structure=ctx))
    # presentation: newest pools first; at most max_pools_per_side ACTIVE per side, plus recent swept ones
    out_pools, hidden = [], {"BSL": 0, "SSL": 0, "expired": 0}
    for side in ("BSL", "SSL"):
        side_pools = sorted((p for p in pools if p.side == side), key=lambda q: -q.created_index)
        active = [p for p in side_pools if p.state == ACTIVE]
        swept = [p for p in side_pools if p.state == SWEPT]
        hidden["expired"] += sum(1 for p in side_pools if p.state == EXPIRED)
        hidden[side] = max(0, len(active) - cfg.max_pools_per_side)
        out_pools += active[:cfg.max_pools_per_side] + swept[:cfg.max_pools_per_side]
    keep = {p.id for p in out_pools}
    return LiquidityResult(config=cfg, pools=tuple(sorted(out_pools, key=lambda q: (q.side, -q.created_index))),
                           sweeps=tuple(s for s in linked if s.pool_id in keep), revision=_revision(bars), candles_used=n, hidden_pools=hidden)


def diff_liquidity(old: dict, new: dict) -> dict:
    """What a correction (or new data) changed between two `LiquidityResult.to_dict()` outputs."""
    def index(d, key):
        return {x["id"]: x for x in d.get(key, [])}
    out = {"revision_changed": old.get("revision") != new.get("revision")}
    for key in ("pools", "sweeps"):
        a, b = index(old, key), index(new, key)
        out[key] = {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
                    "changed": sorted(i for i in set(a) & set(b) if a[i] != b[i])}
    return out
