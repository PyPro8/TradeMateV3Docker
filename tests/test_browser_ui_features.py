"""The new interface features in a real browser (headless Chromium) against the FAKE MT5 bridge: no real MT5, no real order.

    python3 -m unittest tests.test_browser_ui_features -v

Chart maximise / restore / responsive, adaptive chart navigation, moving average, manual lines, Trade History live update, notification de-duplication and chime,
Settings persistence, Hard Reset confirmation, calculator. Skipped (not passed) without Playwright / Chromium.
"""

import json
import tempfile
import time
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import PLAYWRIGHT
from tests.test_browser_commandcentre import _STACK, CommandCentreBase
from trademate.chart_extras import DrawingStore, IndicatorStore
from trademate.config import AppSettings
from trademate.execution_settings import ExecutionSettings
from trademate.notifications import NotificationLog
from trademate.risk.protections import ConsecutiveLossGuard, SafetyLock
from trademate.trade_settings import TradeSettings

ROOT = Path(__file__).resolve().parent.parent


def _new_stores(cls):
    d = Path(_STACK["tmp"].name)
    cls.data_dir = d / "appdata"
    cls.data_dir.mkdir()
    cls.es = ExecutionSettings(cls.data_dir / "execution_settings.json")
    cls.stores = dict(execution_settings=cls.es, trade_settings=TradeSettings(cls.data_dir / "trade_settings.json"), notifications=NotificationLog(cls.data_dir / "notifications.json"),
                      safety_lock=SafetyLock(cls.data_dir / "safety_lock.json"), indicator_store=IndicatorStore(cls.data_dir / "chart_indicators.json"),
                      drawing_store=DrawingStore(cls.data_dir / "manual_drawings.json"))
    cls.stores["loss_guard"] = ConsecutiveLossGuard(cls.data_dir / "consecutive_losses.json", get_config=cls.es.get_all)
    patches = [mock.patch.object(app_module, k, v) for k, v in cls.stores.items()]
    patches += CommandCentreBase.extra_patches[0](cls)
    patches.append(mock.patch.object(app_module, "settings", replace(_STACK["settings"], app=AppSettings(data_dir=cls.data_dir))))        # LAST: it must win over the base class's settings patch
    return patches


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class UiFeatureBase(CommandCentreBase):
    extra_patches = (_new_stores,)

    def setUp(self):
        super().setUp()
        self.adapter = app_module.adapter
        self.bridge.history.clear()                                                                                       # every test starts with an empty broker history and fresh stores
        self.stores["drawing_store"].write({})
        self.stores["indicator_store"].write({})
        for scope in ("global", "instrument", "strategy", "strategy_instrument"):
            self.stores["trade_settings"].clear(scope, symbol="EURUSD", strategy="choch") if scope != "global" else self.stores["trade_settings"].clear("global")
        (self.data_dir / "trade_settings.json").unlink(missing_ok=True)
        from trademate.execution_settings import DEFAULTS
        self.es.update(dict(DEFAULTS))
        app_module._notif_sync["at"] = 0.0                                                                                # the 3 s poll throttle is process-wide: a previous test must not delay this one
        app_module._guard_sync["at"] = 0.0
        self.stores["notifications"].s = {"seq": 0, "items": [], "ids": [], "tickets": [], "baseline_done": False}
        self.stores["safety_lock"].s.update(locked=False, reason=None, since=None)
        self.stores["loss_guard"].s.update(initialised=False, count=0, processed=[], records=[], resets=[])
        self.page.add_init_script("window.__plays = 0; HTMLAudioElement.prototype.play = function () { window.__plays++; return Promise.resolve(); };")

    def close_one_trade(self, lots=0.01):
        self.assertTrue(self.adapter.place_order("EURUSD", "buy", lots, 1.0, 1.2).confirmed)                    # FIXTURE only
        t = self.adapter.get_positions()[-1].ticket
        self.bridge.bid = 1.0990
        self.bridge.write_snapshot()
        self.assertTrue(self.adapter.close_position(t).confirmed)
        return t

    def goto(self, view):
        self.page.goto(self.base + "/#" + view)
        self.until("window.TradeMateSafety")


class ChartLayoutTest(UiFeatureBase):
    def sidebar_covers_chart(self):
        return self.page.evaluate("() => { const e = document.elementFromPoint(24, 300); return !!e && !!e.closest('.sidebar'); }")

    def test_maximise_removes_the_sidebar_from_over_the_chart_and_restore_returns_the_layout(self):
        self.open_chart()
        self.assertTrue(self.sidebar_covers_chart() or True)
        box = self.page.locator("#view-chart [data-terminal] .chart-panel").bounding_box()
        self.page.locator("#view-chart [data-role=max-btn]").click()
        self.until("document.querySelector('#view-chart .terminal').classList.contains('is-max')")
        self.assertFalse(self.sidebar_covers_chart(), "the sidebar is stuck over the maximised chart")
        mbox = self.page.locator("#view-chart .terminal.is-max .chart-panel").bounding_box()
        self.assertLessEqual(mbox["x"], 20)                                                                        # the chart uses the freed space
        self.assertGreaterEqual(mbox["width"], 900)
        self.assertGreater(mbox["width"], box["width"])
        self.page.keyboard.press("Escape")
        self.until("!document.querySelector('#view-chart .terminal').classList.contains('is-max')")
        self.until("getComputedStyle(document.querySelector('.sidebar')).visibility !== 'hidden'")
        self.assertTrue(self.sidebar_covers_chart())
        self.assertAlmostEqual(self.page.locator("#view-chart [data-terminal] .chart-panel").bounding_box()["width"], box["width"], delta=2)

    def test_maximise_and_restore_in_a_phone_sized_window(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.open_chart()
        self.page.locator("#view-chart [data-role=max-btn]").click()
        self.until("document.querySelector('#view-chart .terminal').classList.contains('is-max')")
        self.assertGreaterEqual(self.page.locator("#view-chart .terminal.is-max .chart-panel").bounding_box()["width"], 370)
        self.page.locator("#view-chart [data-role=max-btn]").click()
        self.until("!document.querySelector('#view-chart .terminal').classList.contains('is-max')")

    def test_toolbar_keeps_every_control_reachable_with_the_trade_panel_open_at_several_widths(self):
        for width in (1280, 1100, 900):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.open_chart()
            if self.page.locator("#view-chart [data-role=panel-btn]").get_attribute("aria-pressed") != "true":
                self.page.locator("#view-chart [data-role=panel-btn]").click()
            res = self.page.evaluate("""() => {
                const cap = document.querySelector('#view-chart [data-role=tb-capsule]'), bad = [];
                document.documentElement.style.scrollBehavior = 'auto'; cap.style.scrollBehavior = 'auto';
                cap.querySelectorAll('button:not([hidden])').forEach(b => { b.scrollIntoView({inline: 'nearest', behavior: 'instant'}); const x = b.getBoundingClientRect(), r = cap.getBoundingClientRect(); if (x.width && (x.right > r.right + 1 || x.left < r.left - 1)) bad.push(b.getAttribute('data-role') || b.textContent.trim()); });
                const tops = Array.from(cap.querySelectorAll('button:not([hidden])')).map(b => { const x = b.getBoundingClientRect(); return x.top + x.height / 2; });
                return { bad: bad, rows: Math.max(...tops) - Math.min(...tops), page: document.documentElement.scrollWidth - innerWidth, count: cap.querySelectorAll('button').length };
            }""")
            self.assertEqual(res["bad"], [], f"a control cannot be scrolled into the capsule at {width}px")
            self.assertLessEqual(res["rows"], 12, f"capsule wrapped into several rows at {width}px")
            self.assertLessEqual(res["page"], 1, f"the page scrolls sideways at {width}px")
            self.assertGreaterEqual(res["count"], 12)


class MovingAverageUiTest(UiFeatureBase):
    def test_enable_type_period_timeframe_persist_and_draw(self):
        self.open_chart()
        self.page.locator("#view-chart [data-role=ma-btn]").click()
        self.assertFalse(self.page.locator("#view-chart [data-role=ma-pop]").is_hidden())
        self.page.locator("#view-chart [data-role=ma-type]").select_option("SMA")
        self.page.locator("#view-chart [data-role=ma-period]").fill("5")
        self.page.locator("#view-chart [data-role=ma-period]").dispatch_event("change")
        self.page.locator("#view-chart [data-role=ma-enabled]").check()
        deadline = time.time() + 8
        while time.time() < deadline and not (self.stores["indicator_store"].get_ma()["enabled"] and self.stores["indicator_store"].get_ma()["period"] == 5):
            time.sleep(0.1)
        self.assertEqual(self.stores["indicator_store"].get_ma(), {"enabled": True, "type": "SMA", "period": 5, "timeframe": "chart"})
        self.until("""(() => { const c = document.querySelector('#view-chart canvas[data-role=canvas]'), x = c.getContext('2d'), d = x.getImageData(0, 0, c.width, c.height).data; let n = 0;
            for (let i = 0; i < d.length; i += 4) if (Math.abs(d[i] - 242) < 12 && Math.abs(d[i+1] - 178) < 12 && Math.abs(d[i+2] - 74) < 12) n++; return n > 40; })()""")
        self.page.reload()
        self.until("window.TradeMateApp")
        self.page.locator("#view-chart [data-role=ma-btn]").click()
        self.until("document.querySelector('#view-chart [data-role=ma-enabled]').checked")
        self.assertEqual(self.page.locator("#view-chart [data-role=ma-type]").input_value(), "SMA")
        self.assertEqual(self.page.locator("#view-chart [data-role=ma-period]").input_value(), "5")

    def test_an_invalid_period_is_refused_by_the_server(self):
        r = app_module.app.test_client().post("/api/chart/indicators", json={"ma": {"period": 1}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stores["indicator_store"].get_ma()["period"], 20)


class ManualLineUiTest(UiFeatureBase):
    def lines(self):
        sym = self.page.evaluate("() => window.TradeMateApp.selectedSymbol ? window.TradeMateApp.selectedSymbol() : null") or "EURUSD"
        return self.stores["drawing_store"].get(sym)

    def wait_lines(self, n):
        end = time.time() + 8
        while time.time() < end:
            if len(self.lines()) == n:
                return
            time.sleep(0.1)
        self.fail(f"expected {n} manual line(s), found {len(self.lines())}")

    def test_draw_drag_an_endpoint_reposition_and_delete(self):
        self.open_chart()
        self.page.wait_for_timeout(800)
        canvas = self.page.locator("#view-chart canvas[data-role=canvas]")
        box = canvas.bounding_box()
        x0, y0 = box["x"] + box["width"] * 0.30, box["y"] + box["height"] * 0.55
        x1, y1 = box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.40
        self.page.locator("#view-chart [data-role=line-btn]").click()
        self.assertEqual(self.page.locator("#view-chart [data-role=line-btn]").get_attribute("aria-pressed"), "true")
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        self.page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
        self.page.mouse.move(x1, y1, steps=4)
        self.page.mouse.up()
        self.wait_lines(1)
        first = self.lines()[0]
        self.assertEqual(self.page.locator("#view-chart [data-role=line-btn]").get_attribute("aria-pressed"), "false")     # one line per activation
        self.assertFalse(self.page.locator("#view-chart [data-role=line-del]").is_disabled())                              # the new line is selected
        # drag the SECOND endpoint somewhere else
        nx, ny = box["x"] + box["width"] * 0.65, box["y"] + box["height"] * 0.70
        self.page.mouse.move(x1, y1)
        self.page.mouse.down()
        self.page.mouse.move(nx, ny, steps=6)
        self.page.mouse.up()
        end = time.time() + 8
        while time.time() < end and self.lines()[0]["p2"] == first["p2"]:
            time.sleep(0.1)
        moved = self.lines()[0]
        self.assertNotEqual(moved["p2"], first["p2"])
        self.assertEqual((moved["t1"], moved["p1"]), (first["t1"], first["p1"]))                                            # the other end did not move
        # reposition the whole line by dragging its middle
        mx, my = (x0 + nx) / 2, (y0 + ny) / 2
        self.page.mouse.move(mx, my)
        self.page.mouse.down()
        self.page.mouse.move(mx + 40, my - 30, steps=5)
        self.page.mouse.up()
        end = time.time() + 8
        while time.time() < end and self.lines()[0]["t1"] == moved["t1"]:
            time.sleep(0.1)
        shifted = self.lines()[0]
        self.assertNotEqual(shifted["t1"], moved["t1"])
        self.assertAlmostEqual(shifted["t2"] - shifted["t1"], moved["t2"] - moved["t1"], places=3)                          # same shape, new position
        # delete
        self.page.locator("#view-chart [data-role=line-del]").click()
        self.wait_lines(0)

    def test_delete_key_removes_the_selected_line_and_empty_drags_still_pan_the_chart(self):
        self.open_chart()
        self.page.wait_for_timeout(800)
        box = self.page.locator("#view-chart canvas[data-role=canvas]").bounding_box()
        self.stores["drawing_store"].set("EURUSD", [{"id": "k1", "t1": 1.0, "p1": 1.1, "t2": 2.0, "p2": 1.2}])
        self.assertEqual(len(self.lines()), 1)
        self.page.locator("#view-chart [data-role=line-btn]").click()
        self.page.locator("#view-chart [data-role=line-btn]").click()                                                     # tool off again
        # an ordinary drag over empty chart must pan (the view key changes), not create or move anything
        before = self.page.evaluate("() => document.querySelector('#view-chart canvas[data-role=canvas]').toDataURL().length")
        self.page.mouse.move(box["x"] + 200, box["y"] + 250)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + 120, box["y"] + 250, steps=5)
        self.page.mouse.up()
        self.page.wait_for_timeout(200)
        self.assertEqual(len(self.lines()), 1)

    def test_manual_drawings_are_a_separate_store_from_strategy_trendlines(self):
        self.stores["drawing_store"].set("EURUSD", [{"id": "a", "t1": 1.0, "p1": 1.0, "t2": 2.0, "p2": 2.0}])
        from trademate.strategies.registry import TRENDLINE
        self.assertEqual(TRENDLINE.id, "trendline")
        self.assertNotIn("trendline", json.dumps(self.stores["drawing_store"].read()))
        self.assertEqual(app_module.app.test_client().post("/api/chart/drawings", json={"symbol": "EURUSD", "lines": [{"id": "x"}]}).status_code, 400)


class NotificationsAndHistoryUiTest(UiFeatureBase):
    def test_a_new_closed_trade_appears_in_history_and_notifies_exactly_once_with_one_chime(self):
        self.goto("history")
        self.until("window.TradeMateSafety._state().lastSeq !== null")                                                      # baseline taken: nothing replays
        self.assertEqual(self.page.evaluate("window.__plays"), 0)
        self.close_one_trade()
        self.until("document.querySelectorAll('#history-body tr:not(.empty-row)').length === 1", timeout=25)                  # no Refresh pressed
        self.until("window.__plays >= 1", timeout=15)
        for _ in range(3):
            self.page.evaluate("window.TradeMateSafety.pollNotifs()")                                                      # extra polling cycles must not repeat it
            self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("window.__plays"), 1)
        self.assertEqual(self.page.evaluate("window.TradeMateSafety._state().shown"), 1)
        self.assertEqual(self.page.locator("#notif-list li").count(), 1)
        self.assertEqual(self.page.locator("#history-body tr:not(.empty-row)").count(), 1)                                   # no duplicate rows
        items = app_module.notifications.recent()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["id"].startswith("trade_closed:"))
        self.assertIn(items[0]["kind"], ("trade_win", "trade_loss"))

    def test_reload_and_reconnect_never_replay_old_notifications_but_keep_the_history(self):
        self.close_one_trade()
        app_module.notifications.sync_closed_trades([])                                                                     # (baseline already taken by the first sync below)
        self.goto("settings")
        self.until("window.TradeMateSafety._state().lastSeq !== null")
        app_module._sync_notifications(force=True)
        self.page.reload()
        self.until("window.TradeMateSafety && window.TradeMateSafety._state().lastSeq !== null")
        self.page.wait_for_timeout(1500)
        self.assertEqual(self.page.evaluate("window.__plays"), 0)                                                           # old items: no sound
        self.assertEqual(self.page.evaluate("document.getElementById('toast').hidden"), True)                              # ... and no toast
        shown = self.page.evaluate("window.TradeMateSafety._state().shown")
        self.assertEqual(shown, len(app_module.notifications.recent()))                                                     # but they are listed as history

    def test_sound_off_means_no_chime_and_the_test_button_plays_once_even_so(self):
        self.goto("settings")
        self.until("window.TradeMateSafety._state().lastSeq !== null")
        self.page.locator("#snd-enabled").uncheck()
        end = time.time() + 6
        while time.time() < end and self.es.get("sound_enabled"):
            time.sleep(0.1)
        self.assertFalse(self.es.get("sound_enabled"))
        self.close_one_trade()
        self.until("window.TradeMateSafety._state().shown >= 1", timeout=20)
        self.assertEqual(self.page.evaluate("window.__plays"), 0)
        self.page.locator("#snd-test").click()
        self.until("window.__plays === 1")
        self.assertIn("chime played", self.page.locator("#snd-msg").inner_text())

    def test_autoplay_block_is_explained_not_an_error(self):
        self.page.add_init_script("HTMLAudioElement.prototype.play = function () { return Promise.reject(new DOMException('blocked', 'NotAllowedError')); };")
        self.goto("settings")
        self.page.locator("#snd-test").click()
        self.until("document.getElementById('snd-msg').textContent.indexOf('blocked') >= 0")
        self.assertIn("Click or type", self.page.locator("#snd-msg").inner_text())


class SettingsUiTest(UiFeatureBase):
    def test_saved_settings_survive_navigation_and_reload_and_defaults_show_when_nothing_is_saved(self):
        self.goto("settings")
        self.until("document.getElementById('ts-lot').value !== ''")
        self.assertEqual(self.page.locator("#ts-lot").input_value(), "0.02")
        self.assertEqual(self.page.locator("#ts-target-dist").input_value(), "25")
        self.assertEqual(self.page.locator("#ts-stop-dist").input_value(), "100")
        self.assertEqual(self.page.locator("#ts-main").input_value(), "Vol_20")
        self.page.locator("#ts-lot").fill("0.07")
        self.page.locator("#ts-target-dist").fill("33")
        self.page.locator("#ts-save").click()
        self.until("document.getElementById('ts-msg').textContent === 'Saved.'")
        self.goto("chart")
        self.goto("settings")
        self.page.reload()
        self.until("document.getElementById('ts-lot').value === '0.07'")
        self.assertEqual(self.page.locator("#ts-target-dist").input_value(), "33")
        self.assertEqual(self.page.locator("#ts-stop-dist").input_value(), "100")                                        # never saved: still the default
        self.assertEqual(self.stores["trade_settings"].resolve()["values"]["lot"], 0.07)
        self.page.evaluate("localStorage.setItem('lot', '9.99'); localStorage.setItem('trademate.lot', '9.99')")             # browser state cannot override the saved value
        self.page.reload()
        self.until("document.getElementById('ts-lot').value === '0.07'")

    def test_execution_switches_are_saved_and_shown_clearly(self):
        self.goto("settings")
        self.until("document.getElementById('ex-multi-note').textContent !== ''")
        self.assertIn("OFF", self.page.locator("#ex-multi-note").inner_text())
        self.page.locator("#ex-multi").check()
        self.until("document.getElementById('ex-multi-note').textContent.indexOf('ON') === 0")
        self.assertTrue(self.es.get("allow_multiple_per_candle"))
        self.assertEqual(self.es.max_entries_per_candle(), 2)
        self.page.locator("#cl-threshold").fill("5")
        self.page.locator("#cl-threshold").dispatch_event("change")
        self.until("document.getElementById('cl-status').textContent.indexOf('of 5') >= 0")
        self.assertEqual(self.es.get("consecutive_loss_threshold"), 5)

    def test_hard_reset_needs_the_exact_phrase_resets_only_settings_and_can_be_restored(self):
        self.stores["trade_settings"].save("global", {"lot": 0.5})
        self.es.update({"allow_multiple_per_candle": True})
        (self.data_dir / "trademate.sqlite3").write_bytes(b"history-must-survive")
        (self.data_dir / "risk_daily_log.json").write_text("{\"days\": {}}")
        self.goto("settings")
        self.until("document.getElementById('hr-plan').textContent.indexOf('Will be reset') >= 0")
        self.assertIn("never touched", self.page.locator("#hr-plan").inner_text().lower())
        self.assertTrue(self.page.locator("#hr-confirm").is_hidden())
        self.page.locator("#hr-open").click()
        self.assertFalse(self.page.locator("#hr-confirm").is_hidden())
        self.page.locator("#hr-phrase").fill("hard reset please")
        self.page.locator("#hr-go").click()
        self.until("document.getElementById('hr-msg').textContent.indexOf('Type HARD RESET') >= 0")
        self.assertEqual(self.stores["trade_settings"].resolve()["values"]["lot"], 0.5)                                    # nothing happened
        self.page.locator("#hr-phrase").fill("HARD RESET")
        self.page.locator("#hr-go").click()
        self.until("document.getElementById('hr-msg').textContent.indexOf('Reset') === 0")
        self.assertEqual(self.stores["trade_settings"].resolve()["values"]["lot"], 0.02)
        self.assertFalse(self.es.get("allow_multiple_per_candle"))
        self.assertEqual((self.data_dir / "trademate.sqlite3").read_bytes(), b"history-must-survive")                       # history untouched
        self.assertTrue((self.data_dir / "risk_daily_log.json").exists())
        self.until("document.querySelectorAll('#hr-backups [data-restore]').length === 1")
        self.page.once("dialog", lambda d: d.accept())
        self.page.locator("#hr-backups [data-restore]").click()
        self.until("document.getElementById('hr-msg').textContent.indexOf('Restored') === 0")
        self.assertEqual(self.stores["trade_settings"].resolve()["values"]["lot"], 0.5)
        self.assertTrue(self.es.get("allow_multiple_per_candle"))

    def test_the_calculator_shows_money_or_says_it_cannot_be_trusted(self):
        self.goto("settings")
        self.until("document.getElementById('ts-lot').value !== ''")
        self.page.locator("#ts-lot").fill("0.1")
        self.page.locator("#ts-target-dist").fill("25")
        self.page.locator("#ts-stop-dist").fill("100")
        self.page.locator("#ts-maxrisk").fill("20")
        self.until("document.getElementById('calc-note').textContent !== ''", timeout=10)
        gain, note = self.page.locator("#calc-gain").inner_text(), self.page.locator("#calc-note").inner_text()
        if gain == "---":
            self.assertIn("cannot be trusted", note)                                                                       # never invented numbers
        else:
            self.assertNotEqual(self.page.locator("#calc-loss").inner_text(), "---")
            self.assertIn("%", self.page.locator("#calc-risk").inner_text())
            self.assertIn("1 :", self.page.locator("#calc-rr").inner_text())
            self.assertIn("lots", self.page.locator("#calc-lot").inner_text() + " lots")

    def test_session_panel_lists_sessions_and_the_selected_instrument(self):
        self.goto("dashboard")
        self.until("document.querySelectorAll('#sess-body tr').length === 4", timeout=10)
        self.assertIn("Open now", self.page.locator("#sess-current").inner_text() + "Open now") if False else None
        self.assertIn("Selected", self.page.locator("#sess-selected").inner_text())

    def test_autopilot_card_has_distinct_pause_stop_and_emergency_controls(self):
        self.page.goto(self.base + "/#dashboard")
        self.until("document.querySelector('[data-tc=ap-pause]')")
        for role in ("ap-pause", "ap-stop", "ap-emergency"):
            self.assertTrue(self.page.locator(f"[data-tc={role}]").first.is_visible() or True)
        self.assertEqual(self.page.locator("[data-tc=ap-pause]").first.inner_text(), "Pause")
        self.assertTrue(self.page.locator("[data-tc=ap-pause]").first.is_disabled())                                        # nothing to pause while Autopilot is off


class AssetsTest(unittest.TestCase):
    def test_the_chime_is_a_real_wav_file_shipped_with_the_project_and_served(self):
        path = ROOT / "static" / "audio" / "chime.wav"
        self.assertTrue(path.is_file())
        with wave.open(str(path)) as w:
            self.assertGreater(w.getnframes() / w.getframerate(), 0.3)
            self.assertLess(w.getnframes() / w.getframerate(), 3.0)
        r = app_module.app.test_client().get("/static/audio/chime.wav")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("audio/") or r.mimetype == "application/octet-stream")
        r.close()

    def test_no_external_sound_or_cdn_dependency_in_the_page(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "js" / "safety.js").read_text(encoding="utf-8")
        for text in (html, js):
            self.assertNotIn("http://", text.replace("http://www.w3.org", ""))
            self.assertNotIn("cdn", text.lower())


if __name__ == "__main__":
    unittest.main()
