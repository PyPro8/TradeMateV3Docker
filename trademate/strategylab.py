"""Command Centre capability registry.

The page must never look operational when its backend is not. Every capability has ONE honest state, taken from this registry, and the page
renders the registry; it does not decide anything itself. When a later part implements a capability, its state is changed HERE (and its test),
and the page follows. States:

    Available            works now, end to end, in the Command Centre
    Analysing            an analysis is running                       (set by the workflow, not by this registry)
    Monitoring           a watchdog is running                        (set by the workflow, not by this registry)
    Waiting for Setup    monitoring, nothing qualifies yet            (set by the workflow, not by this registry)
    Disconnected         needs the broker connection and it is down
    Unavailable          cannot work right now (reason given)
    Not Yet Implemented  planned, no working backend in the Command Centre yet

This module has no side effects and no access to any order, sizing or analysis function.
"""

from __future__ import annotations

AVAILABLE, ANALYSING, MONITORING, WAITING = "Available", "Analysing", "Monitoring", "Waiting for Setup"
DISCONNECTED, UNAVAILABLE, NOT_YET = "Disconnected", "Unavailable", "Not Yet Implemented"
STATES = (AVAILABLE, ANALYSING, MONITORING, WAITING, DISCONNECTED, UNAVAILABLE, NOT_YET)

IMPLEMENTED_PART = 11      # latest implemented Command Centre roadmap part


def capabilities(broker_connected: bool, watchdog_state: str | None = None) -> list[dict]:
    """The state of each Command Centre capability. `broker_connected` is the REAL adapter state."""
    def row(cid, label, state, detail, part=None):
        return {"id": cid, "label": label, "state": state, "detail": detail, "part": part}

    broker_state = AVAILABLE if broker_connected else DISCONNECTED
    return [
        row("broker", "Broker connection", broker_state,
            "MT5 is connected with fresh data." if broker_connected else "MT5 is not connected, so nothing that needs market data can run."),
        row("selector", "Market selector", broker_state,
            "Instrument search uses the connected broker's catalogue." if broker_connected else "Connect MT5 to search its instrument catalogue.", 5),
        row("analyse", "ANALYSE", broker_state,
            "Runs the read-only Strategy Engine on live broker candles." if broker_connected else "Connect MT5 before running analysis.", 5),
        row("strategy", "Strategy selection", AVAILABLE,
            "Choose CHoCH Trading or Trend Continuation. The choice is validated by the server, saved, and drives analysis, trade review and the Watchdog."),
        row("mtf", "H1 context and M5 setup", broker_state,
            "The selected strategy reads native H1 (context) and M5 (setup and confirmation) candles only." if broker_connected else "Native timeframe data requires a connected broker.", 6),
        row("decision", "Strategy decision panel", broker_state,
            "BUY, SELL, WAIT or WATCH decisions come from the Strategy Engine response." if broker_connected else "Decision unavailable while the broker is disconnected.", 9),
        row("setup", "Setup details and invalidation", broker_state,
            "Levels and invalidation are shown only when supported by the engine's evidence." if broker_connected else "Setup data requires a connected broker.", 8),
        row("watchdog", "Watchdog", watchdog_state if watchdog_state in (MONITORING, WAITING, DISCONNECTED, UNAVAILABLE) else broker_state,
            "Read-only analysis runs every 60 seconds for the selected instrument." if watchdog_state in (MONITORING, WAITING) else
            "Starts or stops a single-symbol, read-only monitor." if broker_connected else "Connect MT5 before starting monitoring.", 10),
        row("notifications", "Setup notifications", AVAILABLE,
            "Browser alerts are opt-in and require notification permission.", 11),
        row("sound", "Sound", AVAILABLE,
            "Setup sounds are opt-in and use this browser only.", 11),
        row("execution", "Trade review and execution", broker_state,
            "REVIEW TRADE sizes a qualified setup with the Risk Engine and sends nothing; CONFIRM & EXECUTE then re-validates it and uses the existing MT5 pathway."
            if broker_connected else "Connect MT5 before reviewing or executing a trade.", None),
        row("autopilot", "Autopilot", AVAILABLE, "Executes only risk-approved setups of the selected strategy; needs the Watchdog running. Controlled from Trading controls.", None),
    ]
