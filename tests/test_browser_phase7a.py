"""Phase 7A: REAL browser checks (headless Chromium via Playwright) of candle colours, persistence, the structure
overlay, theme independence and the mobile layout.

The page is served by the real Flask app with a stub broker (candles only, no MT5, nothing can trade) and a
temporary settings file. Colours are verified on the actual canvas pixels.

Skipped (and reported as SKIPPED, not passed) when Playwright or Chromium is not installed:
    pip install playwright && playwright install chromium
    python3 -m unittest tests.test_browser_phase7a -v
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    _PLAYWRIGHT = False

import app as app_module
from tests.test_structure_engine import _walk
from trademate.broker.base import (AccountInfo, Candle, ConnectionState, ConnectionStatus, SymbolInfo)
from trademate.chart_settings import ChartSettingsStore
from trademate.errors import BrokerUnavailableError

CANDLE_COUNT = 160


class StubBroker:
    """Connected, read-only, candles only. No order method exists, so nothing can be sent anywhere."""

    def __init__(self):
        self.series = {"M15": _walk(CANDLE_COUNT, 21), "H1": _walk(CANDLE_COUNT, 5)}
        for tf, rows in self.series.items():     # let the live tick leave the newest candle unchanged
            self.last_close = rows[-1][4]

    def get_connection_status(self):
        return ConnectionStatus(ConnectionState.CONNECTED, "stub", 0.1)

    def get_account(self):
        return AccountInfo(1, "stub", "stub", "demo", "USD", 100, 1000.0, 1000.0, 0.0, 1000.0, None, 0.0)

    def get_positions(self):
        return []

    def get_symbols(self):
        bid = self.series["H1"][-1][4]
        return [SymbolInfo("EURUSD", "Euro", 2, 0.01, 2, 100000.0, 0.01, 100.0, 0.01, "EUR", "USD", bid, bid, 10, 0, 4,
                           tick_size=0.01, tick_value=1.0)]

    def get_candles(self, symbol, timeframe, count=300):
        if symbol != "EURUSD" or timeframe.upper() not in self.series:
            raise BrokerUnavailableError("no candles")
        return [Candle(*c) for c in self.series[timeframe.upper()]]

    def get_symbol_spec(self, symbol):
        raise BrokerUnavailableError("no spec")

    def set_watchlist(self, symbols):        # the page syncs its watch list; nothing to do for a stub
        return None

    def get_history(self, days=30, limit=300):
        return []


# Text-contrast audit run inside the page: every visible text node against its effective background (WCAG 4.5:1,
# 3:1 for large text). Gradient-filled buttons are checked by eye / screenshots, not here (they have no background-colour).
AUDIT_JS = r"""() => {
 function parse(c){const m=c.match(/rgba?\(([^)]+)\)/); if(!m) return null; const p=m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [p[0],p[1],p[2],p.length>3?p[3]:1];}
 function over(top,bot){const a=top[3]; return [top[0]*a+bot[0]*(1-a),top[1]*a+bot[1]*(1-a),top[2]*a+bot[2]*(1-a),1];}
 function lum(c){const f=v=>{v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)}; return .2126*f(c[0])+.7152*f(c[1])+.0722*f(c[2]);}
 const light = document.documentElement.getAttribute('data-theme')==='light';
 const base = light ? [243,246,252,1] : [4,8,20,1];
 function bgOf(el){ const chain=[]; for(let e=el;e;e=e.parentElement){const c=parse(getComputedStyle(e).backgroundColor); if(c&&c[3]>0) chain.push(c); if(c&&c[3]===1)break;}
   let bg=base; for(let i=chain.length-1;i>=0;i--) bg=over(chain[i],bg); return bg;}
 const out=[]; const seen=new Set();
 document.querySelectorAll('body *').forEach(el=>{
  if(!el.offsetParent && getComputedStyle(el).position!=='fixed') return;
  const r=el.getBoundingClientRect(); if(r.width<2||r.height<2) return;
  const own=Array.from(el.childNodes).filter(n=>n.nodeType===3&&n.textContent.trim()).map(n=>n.textContent.trim()).join(' ');
  const isField = el.matches('input,select,textarea') ;
  if(!own && !(isField && (el.value||el.placeholder))) return;
  const cs=getComputedStyle(el); const fg=parse(cs.color); if(!fg) return;
  const bg=bgOf(el); const f=fg[3]<1?over(fg,bg):fg;
  const L1=lum(f),L2=lum(bg); const ratio=(Math.max(L1,L2)+.05)/(Math.min(L1,L2)+.05);
  const size=parseFloat(cs.fontSize), bold=parseInt(cs.fontWeight)>=700; const large=size>=24||(size>=18.66&&bold);
  const need=large?3:4.5;
  const dis = el.disabled || el.closest('button:disabled,select:disabled,input:disabled');
  if(ratio<need){ const key=(el.id||el.className||el.tagName)+'|'+own.slice(0,20); if(seen.has(key))return; seen.add(key);
    out.push({tag:el.tagName,cls:String(el.className).slice(0,40),id:el.id,text:(own||el.value||el.placeholder).slice(0,30),ratio:+ratio.toFixed(2),need,fg:cs.color,disabled:!!dis}); }
 });
 return out; }"""


@unittest.skipUnless(_PLAYWRIGHT, "SKIPPED: Playwright is not installed (pip install playwright && playwright install chromium).")
class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls._tmp = tempfile.TemporaryDirectory()
        cls.settings_path = Path(cls._tmp.name) / "chart_settings.json"
        cls.patches = [mock.patch.object(app_module, "adapter", StubBroker()),
                       mock.patch.object(app_module, "chart_store", ChartSettingsStore(cls.settings_path))]
        for p in cls.patches:
            p.start()
        cls.server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # Chromium missing
            cls.pw.stop()
            cls.server.shutdown()
            for p in cls.patches:
                p.stop()
            raise unittest.SkipTest(f"SKIPPED: Chromium could not start ({str(exc)[:80]}).")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        for p in cls.patches:
            p.stop()
        cls._tmp.cleanup()

    def setUp(self):
        if self.settings_path.exists():
            self.settings_path.unlink()
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(self.ctx.close)
        self.post_bodies = []
        self.page.on("request", lambda r: self.post_bodies.append(r.post_data) if r.method == "POST" and "/api/chart/settings" in r.url else None)

    def tearDown(self):
        self.assertEqual(self.errors, [], "JavaScript errors in the page")

    # ---------------- helpers ----------------
    def until(self, expression, timeout=10.0):
        """Poll a JS expression with page.evaluate (the page's strict CSP forbids Playwright's string-eval waits,
        and the CSP stays enforced in these tests on purpose)."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            if self.page.evaluate(f"() => !!({expression})"):
                return
            self.page.wait_for_timeout(100)
        self.fail(f"Timed out waiting for: {expression}")

    def open_chart(self, hash_="#chart"):
        if hash_ in ("#settings", "#dashboard"):  # these views load no candles (the Dashboard's duplicate chart was removed in Vision: one Terminal, on #chart)
            self.page.goto(self.base + "/" + hash_)
        else:
            with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
                self.page.goto(self.base + "/" + hash_)
        self.wait_settings_loaded()
        self.page.wait_for_timeout(300)

    def wait_settings_loaded(self):
        self.until("!document.getElementById('cs-preset').disabled")

    def count(self, rgb):
        """Pixels of exactly this colour on the visible chart canvas."""
        return self.page.evaluate("""(rgb) => {
          const cv = Array.from(document.querySelectorAll('[data-terminal] canvas')).find(c => c.offsetParent !== null);
          const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; let n = 0;
          for (let i = 0; i < d.length; i += 4) if (d[i] === rgb[0] && d[i+1] === rgb[1] && d[i+2] === rgb[2] && d[i+3] === 255) n++;
          return n; }""", list(rgb))

    def wait_count(self, rgb, minimum=100, timeout=8.0):
        """Wait until the chart shows at least `minimum` pixels of this colour (the candles load asynchronously)."""
        import time
        end, last = time.time() + timeout, 0
        while time.time() < end:
            last = self.count(rgb)
            if last >= minimum:
                return last
            self.page.wait_for_timeout(150)
        self.fail(f"chart shows only {last} pixels of {rgb}, expected at least {minimum}")

    def saved(self):
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def wait_saved(self, **expect):
        self.until("document.getElementById('cs-status').textContent === 'Saved.'")
        data = self.saved()
        for k, v in expect.items():
            self.assertEqual(data[k], v)

    # ---------------- tests ----------------
    def test_default_chart_uses_red_and_black_candles_and_is_clean(self):
        self.open_chart()
        self.assertGreater(self.count((0xE5, 0x39, 0x35)), 100)        # bullish: Red
        self.assertGreater(self.count((0x00, 0x00, 0x00)), 100)        # bearish: Black
        self.assertEqual(self.count((0x26, 0xA6, 0x9A)), 0)            # the previous green default is gone
        self.assertEqual(self.count((0x7F, 0xD1, 0xAE)), 0)            # no structure labels by default
        self.assertEqual(self.page.get_attribute("[data-terminal] [data-role='structure-btn']", "aria-pressed"), "false")
        self.assertFalse(self.settings_path.exists())                  # merely viewing never writes settings

    def test_preset_changes_chart_immediately_and_persists_across_reload(self):
        self.open_chart("#settings")
        self.page.select_option("#cs-preset", "violet_ember")
        self.assertEqual(self.page.input_value("#cs-bull-hex"), "#C2A5FF")
        self.assertEqual(self.page.input_value("#cs-bear-hex"), "#FFB86B")
        self.assertEqual(self.page.text_content("#cs-preset-name"), "Violet and Ember")
        self.wait_saved(preset="violet_ember", bull_color="#C2A5FF", bear_color="#FFB86B")
        self.page.evaluate("location.hash = '#chart'")
        self.wait_count((0xC2, 0xA5, 0xFF))
        self.assertEqual(self.count((0x26, 0xA6, 0x9A)), 0)            # the old colour is gone: no reload needed
        # full page reload = restored from the saved file, controls and chart agree
        self.open_chart("#chart")
        self.assertGreater(self.count((0xC2, 0xA5, 0xFF)), 100)
        self.page.evaluate("location.hash = '#settings'")
        self.assertEqual(self.page.input_value("#cs-preset"), "violet_ember")
        self.assertEqual(self.page.input_value("#cs-bull-hex"), "#C2A5FF")
        self.assertEqual(self.page.input_value("#cs-bull-picker"), "#C2A5FF")

    def test_manual_colour_edits_are_independent_and_switch_to_custom(self):
        self.open_chart("#settings")
        self.page.fill("#cs-bull-hex", "#ff0000")
        self.assertEqual(self.page.input_value("#cs-preset"), "custom")
        self.assertEqual(self.page.input_value("#cs-bear-hex"), "#000000")        # bearish untouched
        self.wait_saved(preset="custom", bull_color="#FF0000", bear_color="#000000")
        self.page.fill("#cs-bear-hex", "#00f")                                    # #RGB accepted
        self.until("document.getElementById('cs-bear-picker').value === '#0000ff'")
        self.wait_saved(bull_color="#FF0000", bear_color="#0000FF")
        self.page.evaluate("location.hash = '#chart'")
        self.wait_count((255, 0, 0))
        self.wait_count((0, 0, 255))
        # typing the exact colours of a preset selects that preset
        self.page.evaluate("location.hash = '#settings'")
        self.page.fill("#cs-bull-hex", "#E53935")
        self.page.fill("#cs-bear-hex", "#000000")
        self.assertEqual(self.page.input_value("#cs-preset"), "red_black")

    def test_invalid_hex_changes_nothing(self):
        self.open_chart("#settings")
        self.page.select_option("#cs-preset", "violet_ember")
        self.wait_saved(preset="violet_ember")
        before = self.settings_path.read_bytes()
        n_posts = len(self.post_bodies)
        for bad in ("zzz", "#12", "red", "#fff;}"):
            self.page.fill("#cs-bull-hex", bad)
            self.assertEqual(self.page.get_attribute("#cs-bull-hex", "aria-invalid"), "true")
        self.page.wait_for_timeout(700)
        self.assertEqual(len(self.post_bodies), n_posts)               # nothing was even sent
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(self.page.input_value("#cs-preset"), "violet_ember")

    def test_every_preset_shows_visible_colour_chips(self):
        self.open_chart("#settings")
        chips = self.page.evaluate("""() => Array.from(document.querySelectorAll('.cs-swatch')).map(b =>
            Array.from(b.querySelectorAll('i')).map(i => getComputedStyle(i).backgroundColor))""")
        self.assertEqual(len(chips), 12)
        self.assertIn(["rgb(85, 214, 232)", "rgb(255, 107, 138)"], chips)
        for pair in chips:
            self.assertEqual(len(pair), 2)
            self.assertNotIn("rgba(0, 0, 0, 0)", pair)

    def test_theme_switch_adapts_preset_colors_and_persists_them(self):
        self.open_chart("#settings")
        self.page.select_option("#cs-preset", "arctic_rose")
        self.wait_saved(preset="arctic_rose", bull_color="#55D6E8", bear_color="#FF6B8A")
        before = self.settings_path.read_bytes()
        self.page.evaluate("location.hash = '#chart'")
        self.wait_count((0x55, 0xD6, 0xE8))
        # Phase 7C repair: the chart toolbar's own button is chart-only now, so the APPLICATION-wide switch under test here is the top bar's
        self.page.click("#theme-toggle")
        self.page.wait_for_timeout(300)
        self.wait_count((0x00, 0x7C, 0x91))        # light-theme variant
        self.wait_theme_saved("light")
        after = self.saved()
        before = json.loads(before)
        self.assertEqual(after["theme"], "light")
        self.assertEqual(after["preset"], "arctic_rose")
        self.assertEqual((after["bull_color"], after["bear_color"]), ("#007C91", "#C2185B"))
        self.assertNotEqual((after["bull_color"], after["bear_color"]),
                            (before["bull_color"], before["bear_color"]))

    def test_low_contrast_warning_is_shown_but_colours_are_kept(self):
        self.open_chart("#settings")
        self.page.select_option("#cs-preset", "white_black")
        self.wait_saved(preset="white_black", bull_color="#FFFFFF", bear_color="#000000")
        self.assertTrue(self.page.is_visible("#cs-warning"))           # black on the dark chart
        self.assertIn("bearish", self.page.text_content("#cs-warning"))
        self.assertEqual(self.page.input_value("#cs-bear-hex"), "#000000")
        self.assertEqual(self.saved()["bear_color"], "#000000")
        self.page.select_option("#cs-preset", "silver_flame")
        self.assertFalse(self.page.is_visible("#cs-warning"))

    def test_structure_toggle_shows_hides_and_persists(self):
        self.open_chart()
        labels = [(0x7F, 0xD1, 0xAE), (0xF5, 0x9B, 0xA6), (0x8D, 0xB8, 0xF5), (0xF2, 0xC4, 0x6D)]
        total = lambda: sum(self.count(c) for c in labels)
        self.assertEqual(total(), 0)
        self.page.click("[data-role='structure-btn'] >> visible=true")
        self.until("document.querySelector('#cs-structure').checked === true")
        shown = total()
        self.assertGreater(shown, 20)                                   # labels / levels are really drawn
        self.assertGreater(self.count((0xE5, 0x39, 0x35)), 100)         # candles still there
        self.wait_saved(structure_visible=True)
        self.open_chart()                                               # reload: restored
        self.assertEqual(self.page.get_attribute("[data-role='structure-btn'] >> visible=true", "aria-pressed"), "true")
        self.assertGreater(total(), 20)
        self.page.click("[data-role='structure-btn'] >> visible=true")
        self.page.wait_for_timeout(300)
        self.assertEqual(total(), 0)                                    # hidden again; candles untouched
        self.assertGreater(self.count((0xE5, 0x39, 0x35)), 100)

    def test_overlay_does_not_duplicate_after_refresh_and_follows_timeframe_changes(self):
        self.open_chart()
        self.page.click("[data-role='structure-btn'] >> visible=true")
        self.page.wait_for_timeout(300)
        labels = [(0x7F, 0xD1, 0xAE), (0xF5, 0x9B, 0xA6), (0x8D, 0xB8, 0xF5), (0xF2, 0xC4, 0x6D)]
        total = lambda: sum(self.count(c) for c in labels)
        h1 = total()
        self.assertGreater(h1, 20)
        # two further automatic refreshes of the same data: identical picture
        for _ in range(2):
            with self.page.expect_response(lambda r: "/api/candles" in r.url, timeout=15000):
                pass
            self.page.wait_for_timeout(300)
            self.assertEqual(total(), h1)
        with self.page.expect_response(lambda r: "timeframe=M15" in r.url and "structure=1" in r.url, timeout=15000):
            self.page.click("[data-tf='M15'] >> visible=true")         # another timeframe: its own overlay
        self.page.wait_for_timeout(400)
        m15 = total()
        self.assertGreater(m15, 20)
        self.assertNotEqual(m15, h1)
        with self.page.expect_response(lambda r: "timeframe=H1" in r.url, timeout=15000):
            self.page.click("[data-tf='H1'] >> visible=true")           # back: exactly the first picture again
        self.page.wait_for_timeout(400)
        self.assertEqual(total(), h1)

    def test_resize_keeps_overlay_and_candles_drawn(self):
        self.open_chart()
        self.page.click("[data-role='structure-btn'] >> visible=true")
        self.page.wait_for_timeout(300)
        self.page.set_viewport_size({"width": 700, "height": 800})
        self.page.wait_for_timeout(500)
        self.assertGreater(self.count((0xE5, 0x39, 0x35)), 50)
        self.assertGreater(sum(self.count(c) for c in [(0x7F, 0xD1, 0xAE), (0xF5, 0x9B, 0xA6), (0x8D, 0xB8, 0xF5), (0xF2, 0xC4, 0x6D)]), 10)

    def test_settings_panel_fits_a_phone_screen(self):
        self.page.set_viewport_size({"width": 360, "height": 740})
        self.open_chart("#settings")
        overflow = self.page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        self.assertLessEqual(overflow, 0, "page scrolls sideways on a 360px screen")
        box = self.page.locator("#candle-settings").bounding_box()
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], 360 + 1)
        for sel in ("#cs-bull-hex", "#cs-bear-hex", "#cs-preset", ".cs-swatch >> nth=0"):
            b = self.page.locator(sel).bounding_box()
            self.assertLessEqual(b["x"] + b["width"], 361, sel)

    def test_trade_and_structure_overlays_stay_separate(self):
        # Structure is analysis only: turning it on adds no trade buttons state, no order request is ever made.
        orders = []
        self.page.on("request", lambda r: orders.append(r.url) if "/api/order" in r.url or "/api/position" in r.url else None)
        self.open_chart()
        self.page.click("[data-role='structure-btn'] >> visible=true")
        self.page.wait_for_timeout(600)
        self.assertEqual(orders, [])
        for role in ("quick-buy", "quick-sell"):                         # Task 2: the oversized chart buttons are gone...
            self.assertEqual(self.page.locator(f"[data-role='{role}']").count(), 0)
        for role in ("panel-buy", "panel-sell"):                         # ...and the Trade Panel's own controls are still there, untouched
            self.assertTrue(self.page.locator(f"[data-role='{role}'] >> visible=true").count() >= 1)

    # ---------------- Light / Dark theme (application-wide) ----------------
    def theme(self):
        return self.page.evaluate("() => document.documentElement.getAttribute('data-theme')")

    def toggle_state(self):
        return self.page.get_attribute("#theme-toggle", "aria-checked")

    def wait_theme_saved(self, theme):
        import time
        end = time.time() + 8
        while time.time() < end:
            if self.settings_path.exists() and self.saved().get("theme") == theme:
                return
            self.page.wait_for_timeout(100)
        self.fail(f"theme {theme} was not saved")

    def test_theme_defaults_to_dark_and_toggle_is_an_accessible_switch(self):
        self.open_chart("#dashboard")
        self.assertEqual(self.theme(), "dark")
        self.assertEqual(self.page.get_attribute("#theme-toggle", "role"), "switch")
        self.assertEqual(self.toggle_state(), "false")
        self.assertTrue(self.page.get_attribute("#theme-toggle", "aria-label"))
        self.page.focus("#theme-toggle")                                    # keyboard operable
        self.page.keyboard.press("Enter")
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        self.assertEqual(self.toggle_state(), "true")

    def test_topbar_toggle_switches_the_whole_application(self):
        self.open_chart("#dashboard")
        dark_bg = self.page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        self.page.click("#theme-toggle")
        self.assertEqual(self.theme(), "light")
        self.wait_theme_saved("light")
        light_bg = self.page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        self.assertNotEqual(dark_bg, light_bg)
        lightness = lambda rgb: sum(int(v) for v in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]) / 3
        self.assertGreater(lightness(light_bg), 200)                       # page really is light
        for sel in (".topbar", ".sidebar", ".panel"):
            bg = self.page.evaluate("(s) => getComputedStyle(document.querySelector(s)).backgroundColor", sel)
            self.assertGreater(lightness(bg), 150, sel)
        self.assertEqual(self.page.evaluate("() => getComputedStyle(document.documentElement).colorScheme"), "light")
        self.page.click("#theme-toggle")
        self.assertEqual(self.theme(), "dark")
        self.wait_theme_saved("dark")

    def test_chart_and_settings_controls_stay_in_step_with_the_toggle(self):
        """Updated in the Phase 7C repair. The top-bar toggle and the Settings select are the APPLICATION-wide controls, and a chart left on
        its default "auto" chart theme follows them. The chart toolbar's own button is chart-only now (see test_browser_phase7c_repair)."""
        self.open_chart("#chart")
        self.page.click("#theme-toggle")
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        self.assertEqual(self.toggle_state(), "true")
        self.assertEqual(self.page.get_attribute("[data-terminal] >> nth=0", "data-chart-theme"), "light")
        self.page.evaluate("location.hash = '#settings'")
        self.assertEqual(self.page.input_value("#theme-select"), "light")
        self.page.select_option("#theme-select", "dark")
        self.assertEqual(self.theme(), "dark")
        self.assertEqual(self.toggle_state(), "false")
        self.assertEqual(self.page.get_attribute("[data-terminal] >> nth=0", "data-chart-theme"), "dark")
        self.wait_theme_saved("dark")
        self.page.evaluate("location.hash = '#chart'")
        self.page.click("[data-role='theme-btn'] >> visible=true")          # the chart's OWN button never touches the application theme
        self.page.wait_for_timeout(300)
        self.assertEqual(self.theme(), "dark")
        self.assertEqual(self.toggle_state(), "false")

    def test_theme_persists_across_refresh_and_a_brand_new_browser_profile(self):
        self.open_chart("#dashboard")
        self.page.click("#theme-toggle")
        self.wait_theme_saved("light")
        self.open_chart("#dashboard")                                       # page refresh
        self.assertEqual(self.theme(), "light")
        fresh = self.browser.new_context(viewport={"width": 1280, "height": 900})   # empty localStorage = "restart"
        self.addCleanup(fresh.close)
        page2 = fresh.new_page()
        page2.goto(self.base + "/#dashboard")
        end = __import__("time").time() + 8
        while __import__("time").time() < end and page2.evaluate("() => document.documentElement.getAttribute('data-theme')") != "light":
            page2.wait_for_timeout(100)
        self.assertEqual(page2.evaluate("() => document.documentElement.getAttribute('data-theme')"), "light")
        self.assertEqual(page2.get_attribute("#theme-toggle", "aria-checked"), "true")

    def test_theme_switch_adapts_recommended_palette_without_changing_its_identity(self):
        self.open_chart("#settings")
        self.page.select_option("#cs-preset", "arctic_rose")
        self.wait_saved(preset="arctic_rose")
        colours = {k: self.saved()[k] for k in ("preset", "bull_color", "bear_color")}
        n_before = len(self.post_bodies)
        for _ in range(3):                                                  # dark -> light -> dark -> light -> ...
            self.page.click("#theme-toggle")
            self.page.wait_for_timeout(500)
            self.assertEqual(self.page.input_value("#cs-preset"), "arctic_rose")
        sent = [json.loads(b) for b in self.post_bodies[n_before:]]
        self.assertTrue(sent)
        for body in sent:
            self.assertIn("theme", body)
        self.page.evaluate("location.hash = '#chart'")
        self.wait_count((0x00, 0x7C, 0x91))                                 # adaptive preset follows light theme
        self.assertEqual(self.count((0x26, 0xA6, 0x9A)), 0)

    def test_white_and_black_candles_are_kept_and_warned_about_never_replaced(self):
        self.open_chart("#settings")
        self.page.fill("#cs-bull-hex", "#FFFFFF")
        self.page.fill("#cs-bear-hex", "#000000")
        self.wait_saved(preset="custom", bull_color="#FFFFFF", bear_color="#000000")
        for expected_warning in ("bullish", "bearish"):                      # light: white fails; dark: black fails
            self.page.click("#theme-toggle")
            self.page.wait_for_timeout(400)
            self.assertTrue(self.page.is_visible("#cs-warning"))
            self.assertIn(expected_warning, self.page.text_content("#cs-warning"))
            self.assertEqual((self.saved()["bull_color"], self.saved()["bear_color"]), ("#FFFFFF", "#000000"))
        self.page.evaluate("location.hash = '#chart'")
        self.wait_count((255, 255, 255), 50)
        self.wait_count((0, 0, 0), 50)

    def test_legacy_browser_only_theme_is_migrated_to_the_saved_settings(self):
        self.ctx.close()
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.add_init_script("localStorage.setItem('trademate.chartTheme', JSON.stringify('light'))")
        self.page.goto(self.base + "/#dashboard")
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        self.wait_theme_saved("light")                                      # carried over, not lost

    def test_market_structure_overlay_is_readable_in_the_light_theme(self):
        self.open_chart("#chart")
        self.page.click("#theme-toggle")
        self.page.click("[data-role='structure-btn'] >> visible=true")
        self.page.wait_for_timeout(500)
        light_labels = [(0x0E, 0x7A, 0x54), (0xB3, 0x26, 0x3A), (0x0B, 0x63, 0xD6), (0xA8, 0x68, 0x00)]
        self.assertGreater(sum(self.count(c) for c in light_labels), 20)
        dark_only = [(0x7F, 0xD1, 0xAE), (0xF5, 0x9B, 0xA6), (0x8D, 0xB8, 0xF5), (0xF2, 0xC4, 0x6D)]
        self.assertEqual(sum(self.count(c) for c in dark_only), 0)           # pale dark-theme colours are not used on white
        self.page.click("#theme-toggle")                                    # and the dark palette is back on dark
        self.page.wait_for_timeout(500)
        self.assertGreater(sum(self.count(c) for c in dark_only), 20)

    def test_light_theme_text_has_readable_contrast_on_every_page(self):
        self.open_chart("#dashboard")
        self.page.click("#theme-toggle")
        self.assertEqual(self.theme(), "light")
        problems = []
        for view in ("dashboard", "chart", "strategy", "trades", "history", "journal", "backtest", "settings"):
            self.page.evaluate(f"location.hash = '#{view}'")
            self.page.wait_for_timeout(700)
            for item in self.page.evaluate(AUDIT_JS):
                if "btn-primary" in item["cls"]:      # gradient fill: no background-colour to measure
                    continue
                problems.append((view, item["cls"] or item["tag"], item["text"], item["ratio"]))
        self.assertEqual(problems, [], "low-contrast text in the light theme")

    def test_theme_switch_is_reachable_on_a_phone(self):
        self.page.set_viewport_size({"width": 360, "height": 740})
        self.open_chart("#dashboard")
        box = self.page.locator("#theme-toggle").bounding_box()
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], 360)
        self.page.click("#theme-toggle")
        self.assertEqual(self.theme(), "light")
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 0)

    def test_trade_buttons_stay_readable_and_untouched_by_theme(self):
        self.open_chart("#chart")
        self.page.click("#theme-toggle")
        self.page.wait_for_timeout(300)
        for role in ("panel-buy", "panel-sell"):
            colour = self.page.evaluate("(r) => getComputedStyle(document.querySelector(`[data-role='${r}']`)).backgroundColor", role)
            self.assertIn(colour, ("rgb(0, 0, 139)", "rgb(139, 0, 0)"))     # still darkblue / darkred with white text


if __name__ == "__main__":
    unittest.main()
