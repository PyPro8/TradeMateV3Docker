"""Controlled Risk Engine v5.2 scenarios. No MT5, no orders, no demo trades: synthetic account data only.

    python -m trademate.risk.scenarios
Prints each scenario's decision and exits non-zero if any decision differs from what was expected.
The proposals stand in for what the future Strategy Engine will produce.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from trademate.risk.engine import AccountRiskState, TradeProposal, evaluate_trade, position_risk
from trademate.risk.position_sizing import SymbolSpec
from trademate.risk.rules import RiskRules

START = 52000.0
# A gold-like broker spec: 1 lot loses $1000 per 10.00 of price. Min 0.01, max 50, step 0.01.
GOLD = SymbolSpec("XAUUSD", 0.01, 1.0, 1.0, 0.01, 50.0, 0.01, 4)


def pos(ticket: int, risk_money: float, symbol: str = "XAUUSD", sl: float | None = 2640.0):
    """An open BUY whose stop-loss risk is `risk_money` (open 2650, stop 10.00 away = $1000 per lot)."""
    return position_risk(ticket, symbol, "buy", risk_money / 1000.0, 2650.0, sl, GOLD)


def make_state(lost_percent: float = 0.0, positions=(), start: float = START, history: bool = True) -> AccountRiskState:
    """Account that started the day at `start` and has lost `lost_percent` % of it today (realised)."""
    closed = -start * lost_percent / 100.0
    return AccountRiskState(equity=start + closed, balance=start + closed, currency="USD", start_of_day_balance=start,
                            todays_closed_pl=closed if history else None, floating_pl=0.0, positions=tuple(positions),
                            day_start_source="synthetic", history_error="" if history else "history unavailable")


def proposal(risk_percent: float, direction: str = "buy") -> TradeProposal:
    entry, sl = (2650.0, 2640.0) if direction == "buy" else (2640.0, 2650.0)
    return TradeProposal("XAUUSD", direction, entry, sl, 2670.0 if direction == "buy" else 2620.0, risk_percent)


@dataclass(frozen=True)
class Scenario:
    name: str
    rules: RiskRules
    state: AccountRiskState
    proposal: TradeProposal
    expected: str  # ACCEPTED / REJECTED
    reason_contains: str = ""


R1 = RiskRules(1.0, 3.0, 3.0, 3)
SCENARIOS = [
    Scenario("TEST 1  1% trade, max 1%, nothing else used", R1, make_state(), proposal(1.0), "ACCEPTED"),
    Scenario("TEST 2  1.5% trade, max 1% per trade", R1, make_state(), proposal(1.5), "REJECTED", "maximum risk per trade"),
    Scenario("TEST 3  daily limit 3% already lost, new 0.5% trade", R1, make_state(lost_percent=3.0), proposal(0.5),
             "REJECTED", "Daily loss limit reached"),
    Scenario("TEST 4  lost 2% of 3%, new 1.5% trade (only 1% left)", RiskRules(2.0, 3.0, 10.0, 3), make_state(lost_percent=2.0),
             proposal(1.5), "REJECTED", "remaining daily"),
    Scenario("TEST 4b lost 2% of 3%, new 0.5% trade (still eligible)", R1, make_state(lost_percent=2.0), proposal(0.5), "ACCEPTED"),
    Scenario("TEST 5  3 of max 3 positions already open", R1, make_state(positions=[pos(1, 260), pos(2, 260), pos(3, 260)]),
             proposal(1.0), "REJECTED", "Maximum simultaneous position limit reached"),
    Scenario("TEST 6  active risk 2.5% + new 1% > max 3%", R1, make_state(positions=[pos(1, 650), pos(2, 650)]),
             proposal(1.0), "REJECTED", "Total active risk"),
    Scenario("TEST 7  active risk 1% + new 1% = 2% <= max 3%", R1, make_state(positions=[pos(1, 520)]), proposal(1.0), "ACCEPTED"),
]


def run() -> list[tuple[Scenario, object, bool]]:
    out = []
    for sc in SCENARIOS:
        decision = evaluate_trade(sc.state, sc.proposal, GOLD, sc.rules)
        ok = decision.decision == sc.expected and sc.reason_contains.lower() in (decision.rejection_reason or "").lower()
        out.append((sc, decision, ok))
    return out


def main() -> int:
    bad = 0
    for sc, d, ok in run():
        bad += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {sc.name}\n         -> {d.decision}"
              + (f"  volume {d.approved_volume:g} lots" if d.approved_volume else "") + (f"\n         reason: {d.rejection_reason}" if d.rejection_reason else ""))
    print(f"\n{len(SCENARIOS) - bad}/{len(SCENARIOS)} scenarios behaved as expected.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
