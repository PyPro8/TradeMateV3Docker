"""A FIXTURE that pretends to be the TradeMateBridge EA inside MT5.

It watches the command folder, executes commands against an in-memory account and
writes the snapshot / result files exactly like the real EA does. It lets us test the
TradeMate side of the protocol (command files, expiry, confirmation, error paths)
without MT5. It does NOT prove that real MT5 / Headway accepts anything: that is what
`python -m trademate.broker.live_test` is for.
"""

import json
import threading
import time
from pathlib import Path

SYMBOL = {"name": "EURUSD", "description": "Euro vs US Dollar", "digits": 5, "point": 1e-05, "spread": 12,
          "contract_size": 100000.0, "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
          "stops_level": 10, "freeze_level": 0, "trade_mode": 4,
          "currency_base": "EUR", "currency_profit": "USD", "bid": 1.10000, "ask": 1.10012}


# The broker's catalogue is much bigger than what the snapshot exports (like a real broker).
CATALOGUE = {
    "EURUSD": ("Euro vs US Dollar", "Forex\\Majors", 1.10000, 1.10012, 5, 1e-05, 0.01),
    "XAUUSD": ("Gold vs US Dollar", "Metals\\Spot", 2000.00, 2000.30, 2, 0.01, 0.01),
    "US30.x": ("Wall Street 30 Index", "Indices\\US", 38000.0, 38002.0, 1, 0.1, 0.1),
    "UKOIL": ("Brent Crude Oil", "Energies\\Oil", 80.00, 80.04, 2, 0.01, 0.01),
    "BTCUSD": ("Bitcoin vs US Dollar", "Crypto\\Spot", 60000.0, 60010.0, 2, 0.01, 0.01),
    "CLOSED1": ("Disabled instrument", "Other\\Test", 1.0, 1.1, 5, 1e-05, 0.01),
}


class FakeBridge:
    SERVER = "Headway-Demo"
    PENDING_SUPPORTED = True      # class attribute: tests can switch it off to simulate the OLD EA that has no pending orders      # class attribute so tests can simulate a different / unreported server

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        # write_snapshot() is called by this bridge's own thread AND directly by tests; both used the SAME temp file, so two overlapping
        # calls could make the second rename fail with FileNotFoundError (a pre-existing race, found in the Phase 7C repair). One lock fixes it.
        self._snapshot_lock = threading.Lock()
        self.positions: dict[int, dict] = {}
        self.orders: dict[int, dict] = {}        # pending orders (ticket -> fields)
        self.next_ticket = 1000
        self.bid, self.ask = 1.10000, 1.10012
        self.trade_mode = "demo"
        self.trading_enabled = True
        self.reject_with: tuple[int, str] | None = None   # make the "broker" refuse the next write
        self.silent = False                               # EXECUTE but never answer (the answer is lost)
        self.ignore_commands = False                      # never pick commands up (MT5/EA not running)
        self.swallow_commands = False                     # take the command file, do NOTHING and never answer
        self.apply_but_hide = False                       # accept but never show in the snapshot
        self.commands_seen: list[dict] = []
        self.scripted_candles: dict = {}                  # (symbol, timeframe) -> bars [[time, o, h, l, c], ...] returned verbatim (Candle Momentum tests)
        self.legacy = False                               # behave like EA v0.2 (no symbol commands)
        self.selected = {"EURUSD"}                        # "Market Watch"
        self.server_offset = 0                            # broker clock minus UTC, seconds (history and M1 candles use it)
        self.history: list[dict] = []                     # closed trades (filled when a position is closed)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.write_snapshot()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    # ----- snapshot -----
    def _profit(self, p):
        move = (self.bid - p["price_open"]) if p["type"] == "buy" else (p["price_open"] - self.ask)
        return round(move * 100000 * p["volume"], 2)

    def _exported(self):
        names = [p["symbol"] for p in self.positions.values()]
        watch = self.dir / "trademate_watch.txt"
        if watch.exists():
            names += [x.strip() for x in watch.read_text(encoding="utf-8").splitlines() if x.strip() in CATALOGUE]
        names += sorted(self.selected)
        return list(dict.fromkeys(names))

    def _symbol_json(self, name, detail=False):
        desc, path, bid, ask, digits, point, vmin = CATALOGUE[name]
        if name == "EURUSD":
            bid, ask = self.bid, self.ask
        data = {**SYMBOL, "name": name, "description": desc, "digits": digits, "point": point, "bid": bid,
                "ask": ask, "volume_min": vmin, "volume_step": vmin, "trade_mode": 0 if name == "CLOSED1" else 4}
        if detail:
            data.update(path=path, currency_margin="USD", tick_size=point, tick_value=1.0, tick_value_profit=1.0,
                        tick_value_loss=1.0, visible=name in self.selected)
        return data

    def fill_order(self, ticket):
        """Simulate price reaching a pending order: it stops being an order and becomes a position (same ticket, as in a hedging account)."""
        o = self.orders.pop(ticket)
        self.positions[ticket] = {"symbol": o["symbol"], "type": "buy" if o["type"].startswith("buy") else "sell", "volume": o["volume"],
                                  "price_open": o["price_open"], "sl": o["sl"], "tp": o["tp"]}
        self.write_snapshot()

    def write_snapshot(self):
        positions = []
        if not self.apply_but_hide:
            for t, p in self.positions.items():
                positions.append({**p, "ticket": t, "price_current": self.bid if p["type"] == "buy" else self.ask,
                                  "profit": self._profit(p), "swap": 0.0})
        data = {"schema": 2, "written_at_gmt": int(time.time()),
                "bridge": {"name": "FakeBridge", "version": "test", "read_only": False,
                           "trading_enabled": self.trading_enabled, "uptime_seconds": 500,
                           **({"features": ["pending_orders"]} if self.PENDING_SUPPORTED else {})},
                "terminal": {"connected": True, "name": "MT5", "company": "Test", "build": 1},
                "account": {"login": 11111111, "server": self.SERVER, "company": "Test", "currency": "USD",
                            "leverage": 100, "trade_mode": self.trade_mode, "balance": 1000.0, "equity": 1000.0,
                            "margin": 0.0, "free_margin": 1000.0, "margin_level": 0.0, "profit": 0.0},
                "positions": positions,
                **({"orders": [{**o, "ticket": t} for t, o in self.orders.items()]} if self.PENDING_SUPPORTED else {}),
                "symbols": [self._symbol_json(n) for n in self._exported()]}
        with self._snapshot_lock:
            tmp = self.dir / "trademate_snapshot.json.tmp"
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.dir / "trademate_snapshot.json")

    # ----- commands -----
    def _loop(self):
        last = 0.0
        while not self._stop.is_set():
            for f in sorted(self.dir.glob("trademate_cmd_*.txt")):
                if self.ignore_commands:
                    break
                self._handle(f)
            if time.time() - last > 0.25:
                self.write_snapshot()
                last = time.time()
            time.sleep(0.02)

    def _answer(self, cmd_id, ok, retcode, message, ticket=0, price=0.0, **extra):
        if self.silent:
            return
        body = {"id": cmd_id, "ok": ok, "retcode": retcode, "message": message, "ticket": ticket, "price": price, **extra}
        tmp = self.dir / f"trademate_res_{cmd_id}.tmp"
        tmp.write_text(json.dumps(body), encoding="utf-8")
        tmp.replace(self.dir / f"trademate_res_{cmd_id}.json")

    def _handle(self, path: Path):
        fields = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v
        path.unlink()
        self.commands_seen.append(fields)
        cid, cmd = fields["id"], fields["cmd"]
        if self.swallow_commands and cmd in ("open", "modify", "close", "open_pending", "cancel_order", "modify_order"):
            return
        if int(fields.get("expires", 0)) < time.time() and cmd != "candles":
            return self._answer(cid, False, 0, "Command expired before MT5 could run it.")
        if cmd in ("symbol_info", "symbols_search", "history") and self.legacy:
            return self._answer(cid, False, 0, f"Unknown command '{cmd}'.")
        if cmd == "symbol_info":
            if fields["symbol"] not in CATALOGUE:
                return self._answer(cid, False, 0, f"Symbol '{fields['symbol']}' does not exist at this broker.")
            self.selected.add(fields["symbol"])
            return self._answer(cid, True, 0, "ok", symbol=self._symbol_json(fields["symbol"], True))
        if cmd == "symbols_search":
            q = fields.get("query", "").lower()
            hits = [n for n, v in CATALOGUE.items() if q in n.lower() or q in v[0].lower() or q in v[1].lower()]
            hits.sort(key=lambda n: not n.lower().startswith(q))
            found = [{"name": n, "description": CATALOGUE[n][0], "path": CATALOGUE[n][1],
                      "trade_mode": 0 if n == "CLOSED1" else 4, "visible": n in self.selected}
                     for n in hits[:int(fields["limit"])]]
            return self._answer(cid, True, 0, "ok", matches=len(hits), total_symbols=len(CATALOGUE), found=found)
        if cmd == "history":
            cutoff = time.time() - int(fields.get("days", 30)) * 86400
            # rows are returned as stored, even malformed ones: the adapter must cope with whatever MT5 sends
            rows = [t for t in self.history if not isinstance(t, dict) or t.get("time_close", cutoff) >= cutoff]
            rows.sort(key=lambda t: t.get("time_close", 0) if isinstance(t, dict) else 0, reverse=True)
            return self._answer(cid, True, 0, "ok", trades=rows[:int(fields.get("limit", 200))])
        if cmd == "candles" and (fields["symbol"], fields.get("timeframe")) in getattr(self, "scripted_candles", {}):
            return self._answer(cid, True, 0, "ok", bars=list(self.scripted_candles[(fields["symbol"], fields["timeframe"])]))   # a test scripted the forming candle
        if cmd == "candles":
            import random
            rng, n = random.Random(7), int(fields["count"])
            base = float(self._symbol_json(fields["symbol"])["bid"])
            unit = base * 0.0004
            closes, v = [], base
            for _ in range(n):
                v += rng.uniform(-1, 1) * unit
                closes.append(v)
            shift = base - closes[-1]                      # newest close lands on the current price
            bars, prev = [], closes[0] + shift
            for i, c in enumerate(closes):
                c += shift
                hi = max(prev, c) + rng.uniform(0.1, 0.9) * unit
                lo = min(prev, c) - rng.uniform(0.1, 0.9) * unit
                if fields.get("timeframe") == "M1":
                    # A real broker's newest 1-minute candle opens within the last minute of ITS clock (UTC + server_offset).
                    # TradeMate measures the broker's trading day from exactly this, so the fixture must behave like MT5.
                    open_time = (int(time.time()) + self.server_offset) // 60 * 60 - (n - 1 - i) * 60
                else:
                    open_time = 1700000000 + i * 3600
                bars.append([open_time, round(prev, 5), round(hi, 5), round(lo, 5), round(c, 5)])
                prev = c
            return self._answer(cid, True, 0, "ok", bars=bars)
        if self.trade_mode != "demo":
            return self._answer(cid, False, 0, "REFUSED: this is not a DEMO account.")
        if self.reject_with:
            code, text = self.reject_with
            self.reject_with = None
            return self._answer(cid, False, code, text, int(fields.get("ticket", 0) or 0))
        if cmd == "open":
            self.next_ticket += 1
            if fields["symbol"] not in CATALOGUE:
                return self._answer(cid, False, 4106, "Unknown symbol")
            self.selected.add(fields["symbol"])
            sj = self._symbol_json(fields["symbol"])
            price = sj["ask"] if fields["side"] == "buy" else sj["bid"]
            self.positions[self.next_ticket] = {
                "symbol": fields["symbol"], "type": fields["side"], "volume": float(fields["volume"]),
                "price_open": price, "sl": float(fields["sl"]) or None, "tp": float(fields["tp"]) or None}
            self.write_snapshot()
            return self._answer(cid, True, 10009, "Request executed", self.next_ticket, price)
        if cmd in ("open_pending", "cancel_order", "modify_order") and self.reject_with:
            code, why = self.reject_with
            self.reject_with = None
            return self._answer(cid, False, code, why)
        if cmd == "open_pending":
            if fields["symbol"] not in CATALOGUE:
                return self._answer(cid, False, 4106, "Unknown symbol")
            self.next_ticket += 1
            exp = int(float(fields.get("expiration", 0) or 0))
            if self.apply_but_hide:                       # accepted, but the order never shows up in the account
                return self._answer(cid, True, 10008, "Order placed", self.next_ticket, float(fields["price"]))
            self.orders[self.next_ticket] = {"symbol": fields["symbol"], "type": fields["order_type"], "volume": float(fields["volume"]),
                                             "price_open": float(fields["price"]), "sl": float(fields["sl"]) or None,
                                             "tp": float(fields["tp"]) or None, "expiration": exp or None, "time_setup": int(time.time())}
            self.write_snapshot()
            return self._answer(cid, True, 10008, "Order placed", self.next_ticket, float(fields["price"]))
        if cmd in ("cancel_order", "modify_order"):
            t = int(fields.get("ticket", 0))
            if t not in self.orders:
                return self._answer(cid, False, 0, f"Pending order {t} does not exist.", t)
            if cmd == "cancel_order":
                self.orders.pop(t)
            else:
                o = self.orders[t]
                o["price_open"], o["sl"], o["tp"] = float(fields["price"]), float(fields["sl"]) or None, float(fields["tp"]) or None
                o["expiration"] = int(float(fields.get("expiration", 0) or 0)) or None
            self.write_snapshot()
            return self._answer(cid, True, 10009, "Request executed", t)
        ticket = int(fields.get("ticket", 0))
        if ticket not in self.positions:
            return self._answer(cid, False, 0, f"Position {ticket} is not open (already closed?).", ticket)
        if cmd == "modify":
            self.positions[ticket]["sl"] = float(fields["sl"]) or None
            self.positions[ticket]["tp"] = float(fields["tp"]) or None
        elif cmd == "close":
            p = self.positions.pop(ticket)
            exit_price = self.bid if p["type"] == "buy" else self.ask
            now = int(time.time())
            self.history.append({
                "ticket": ticket, "symbol": p["symbol"], "type": p["type"], "volume": p["volume"],
                "price_open": p["price_open"], "price_close": exit_price, "sl": p["sl"] or 0, "tp": p["tp"] or 0,
                "profit": self._profit({**p, "price_open": p["price_open"]}), "swap": 0.0, "commission": 0.0,
                "time_open": now + self.server_offset - 120, "time_close": now + self.server_offset, "reason": "client"})
        self.write_snapshot()
        self._answer(cid, True, 10009, "Request executed", ticket)
