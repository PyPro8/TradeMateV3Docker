"""Chart appearance settings (Phase 7A): candle colours, colour preset and overlay visibility (market structure; Phase 7B
liquidity; Phase 7C fair value gaps and order blocks), the application theme and the chart's own theme.

Saved in TradeMate's own data folder (data/chart_settings.json), the same mechanism as the risk rules
(data/risk_rules.json). The application theme (`theme`), chart theme (`chart_theme`) and candle colours are stored
separately. The three recommended palettes adapt to the effective chart theme; custom and legacy colors are preserved.

Consistency rule: a non-custom preset ALWAYS has exactly its own two colours for the active chart theme. Colours
are validated strictly (#RGB or #RRGGBB, nothing else) and normalised to upper-case #RRGGBB, so no arbitrary text
can ever reach the page or a stylesheet.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

logger = logging.getLogger("trademate.chart")

HEX_RE = re.compile(r"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})")   # used with fullmatch: no trailing newline, no spaces

CUSTOM = "custom"
# Original presets remain available alongside the new additions.
LEGACY_PRESETS: tuple[tuple[str, str, str, str], ...] = (
    ("red_black", "Red and Black", "#E53935", "#000000"),      # the default (Task 2): bullish candles RED, bearish candles BLACK
    ("classic", "Classic Trading", "#26A69A", "#EF5350"),
    ("purple_black", "Purple and Black", "#800080", "#000000"),
    ("emerald_crimson", "Emerald and Crimson", "#10B981", "#DC2626"),
    ("blue_orange", "Blue and Orange", "#2962FF", "#FF6D00"),
    ("white_black", "White and Black", "#FFFFFF", "#000000"),
    ("green_white", "Green and White", "#00C853", "#FFFFFF"),
    ("gold_navy", "Gold and Navy", "#FFD700", "#001F3F"),
    ("slate_rose", "Slate and Rose", "#64748B", "#F43F5E"),
)
ADDED_PRESETS: tuple[tuple[str, str, str, str], ...] = (
    ("arctic_rose", "Arctic and Rose", "#55D6E8", "#FF6B8A"),
    ("violet_ember", "Violet and Ember", "#C2A5FF", "#FFB86B"),
    ("silver_flame", "Silver and Flame", "#E1E8F5", "#FF6B6B"),
)
PRESETS = LEGACY_PRESETS + ADDED_PRESETS
THEME_PRESET_COLORS = {
    "arctic_rose": {"dark": ("#55D6E8", "#FF6B8A"), "light": ("#007C91", "#C2185B")},
    "violet_ember": {"dark": ("#C2A5FF", "#FFB86B"), "light": ("#6842A6", "#A64B00")},
    "silver_flame": {"dark": ("#E1E8F5", "#FF6B6B"), "light": ("#46556F", "#C62828")},
}
ALL_PRESETS = PRESETS
PRESET_BY_ID = {p[0]: p for p in ALL_PRESETS}
DEFAULT_PRESET = "red_black"
LEGACY_DEFAULT_COLORS = ("#26A69A", "#EF5350")   # the default before Task 2 ("classic"); see ChartSettingsStore.load
PREVIOUS_DEFAULT_COLORS = ("#E53935", "#000000")
THEMES = ("dark", "light")
DEFAULT_THEME = "dark"
CHART_THEMES = ("auto", "dark", "light")      # "auto" = the chart follows the application theme (the default)
DEFAULT_CHART_THEME = "auto"
SWEEP_MODES = ("wick", "close")
# Phase 7B liquidity settings: name -> (type, default, low, high). Validated strictly; one bad value never discards the others.
LIQUIDITY_INT_KEYS = {"liquidity_tolerance_ticks": (5, 0, 500), "liquidity_max_pools": (8, 1, 50)}
ALLOWED_KEYS = {"preset", "bull_color", "bear_color", "structure_visible", "theme", "liquidity_visible", "liquidity_sweep_mode",
                "fvg_visible", "order_blocks_visible", "chart_theme", *LIQUIDITY_INT_KEYS}


@dataclass(frozen=True)
class ChartSettings:
    preset: str = DEFAULT_PRESET
    bull_color: str = PRESET_BY_ID[DEFAULT_PRESET][2]
    bear_color: str = PRESET_BY_ID[DEFAULT_PRESET][3]
    structure_visible: bool = False       # the default chart stays clean
    theme: str = DEFAULT_THEME            # application-wide "dark" | "light"; adaptive presets follow it when chart_theme is auto
    liquidity_visible: bool = False       # liquidity pools / sweeps on the chart (analysis only); the default chart stays clean
    liquidity_tolerance_ticks: int = 5    # swings within this many TICKS of each other count as "equal" (converted with the broker's tick size)
    liquidity_sweep_mode: str = "wick"    # "wick" (high/low crosses the level) | "close" (the candle must close beyond it)
    liquidity_max_pools: int = 8          # most pools shown per side
    fvg_visible: bool = False             # fair value gap zones on the chart (Phase 7C). Display only: hiding them never stops the FVG analysis
    order_blocks_visible: bool = False    # order block zones on the chart. Display only: hiding them never stops the order block analysis
    chart_theme: str = DEFAULT_CHART_THEME   # the chart's own "auto" (follow the application theme) | "dark" | "light"; never changes `theme`
    colors_chosen: bool = False           # True once the user picked a preset or colours. Not settable through the API directly; lets the default change without overriding a choice

    def to_dict(self) -> dict:
        return asdict(self)


def preset_list() -> list[dict]:
    result = []
    for i, n, b, r in PRESETS:
        item = {"id": i, "name": n, "bull_color": b, "bear_color": r}
        if i in THEME_PRESET_COLORS:
            item["theme_colors"] = {
                theme: {"bull_color": colors[0], "bear_color": colors[1]}
                for theme, colors in THEME_PRESET_COLORS[i].items()
            }
        result.append(item)
    return result


def normalize_color(value: object, label: str = "Colour") -> str:
    """'#abc' -> '#AABBCC'. Raises ValueError with a user-facing message."""
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        raise ValueError(f"{label} must be a hexadecimal colour like #26A69A or #2A9.")
    text = value.upper()
    if len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text


def preset_for_colors(bull: str, bear: str) -> str:
    for pid, _name, b, r in ALL_PRESETS:
        if b == bull and r == bear:
            return pid
    for pid, theme_colors in THEME_PRESET_COLORS.items():
        if any((b, r) == (bull, bear) for b, r in theme_colors.values()):
            return pid
    return CUSTOM


def _preset_colors(preset: str, chart_theme: str) -> tuple[str, str]:
    themed = THEME_PRESET_COLORS.get(preset)
    if themed:
        return themed[chart_theme]
    _id, _name, bull, bear = PRESET_BY_ID[preset]
    return bull, bear


def parse_update(data: object, base: ChartSettings) -> ChartSettings:
    """Apply the fields present in `data` on top of `base`; everything else is kept. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object.")
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"Unsupported setting(s): {', '.join(str(k)[:30] for k in unknown)}.")
    theme = data.get("theme", base.theme)
    if theme not in THEMES:
        raise ValueError('theme must be "dark" or "light".')
    chart_theme = data.get("chart_theme", base.chart_theme)
    if chart_theme not in CHART_THEMES:
        raise ValueError('chart_theme must be "auto", "dark" or "light".')
    effective_chart_theme = chart_theme if chart_theme in THEMES else theme
    bull, bear = base.bull_color, base.bear_color
    colors_given = "bull_color" in data or "bear_color" in data
    if "bull_color" in data:
        bull = normalize_color(data["bull_color"], "Bullish colour")
    if "bear_color" in data:
        bear = normalize_color(data["bear_color"], "Bearish colour")

    preset = base.preset
    if "preset" in data:
        wanted = data["preset"]
        if not isinstance(wanted, str) or (wanted != CUSTOM and wanted not in PRESET_BY_ID):
            raise ValueError("Unknown colour preset.")
        if wanted == CUSTOM:
            preset = CUSTOM                      # an explicit choice of Custom stays Custom (colours unchanged)
        else:
            p_bull, p_bear = _preset_colors(wanted, effective_chart_theme)
            if colors_given and (bull, bear) != (p_bull, p_bear):
                raise ValueError(f"The colours do not match the preset '{wanted}'. Send only the preset, or use 'custom'.")
            bull, bear, preset = p_bull, p_bear, wanted
    elif colors_given:
        preset = preset_for_colors(bull, bear)   # manual change: a preset only if the colours match it exactly
    elif preset in THEME_PRESET_COLORS and (("theme" in data) or ("chart_theme" in data)):
        bull, bear = _preset_colors(preset, effective_chart_theme)

    visible = base.structure_visible
    if "structure_visible" in data:
        if not isinstance(data["structure_visible"], bool):
            raise ValueError("structure_visible must be true or false.")
        visible = data["structure_visible"]
    liq_visible = base.liquidity_visible
    if "liquidity_visible" in data:
        if not isinstance(data["liquidity_visible"], bool):
            raise ValueError("liquidity_visible must be true or false.")
        liq_visible = data["liquidity_visible"]
    fvg_visible = base.fvg_visible
    if "fvg_visible" in data:
        if not isinstance(data["fvg_visible"], bool):
            raise ValueError("fvg_visible must be true or false.")
        fvg_visible = data["fvg_visible"]
    ob_visible = base.order_blocks_visible
    if "order_blocks_visible" in data:
        if not isinstance(data["order_blocks_visible"], bool):
            raise ValueError("order_blocks_visible must be true or false.")
        ob_visible = data["order_blocks_visible"]
    mode = base.liquidity_sweep_mode
    if "liquidity_sweep_mode" in data:
        if data["liquidity_sweep_mode"] not in SWEEP_MODES:
            raise ValueError('liquidity_sweep_mode must be "wick" or "close".')
        mode = data["liquidity_sweep_mode"]
    ints = {}
    for key, (_default, lo, hi) in LIQUIDITY_INT_KEYS.items():
        ints[key] = getattr(base, key)
        if key in data:
            v = data[key]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{key} must be a whole number from {lo} to {hi}.")
            ints[key] = v
    chosen = base.colors_chosen or "preset" in data or colors_given
    return ChartSettings(preset=preset, bull_color=bull, bear_color=bear, structure_visible=visible, theme=theme,
                         liquidity_visible=liq_visible, liquidity_sweep_mode=mode, fvg_visible=fvg_visible,
                         order_blocks_visible=ob_visible, chart_theme=chart_theme, colors_chosen=chosen, **ints)


class ChartSettingsStore:
    """Reads / writes data/chart_settings.json. Never touches MT5."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> ChartSettings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            # Field by field: one bad value (say a hand-edited theme) must not throw away the others. A stored preset
            # must agree with the stored colours; if the file was edited into a conflict, the colours win.
            merged = ChartSettings()
            for part in (("bull_color", "bear_color"), ("structure_visible",), ("theme",), ("liquidity_visible",),
                         ("liquidity_sweep_mode",), ("liquidity_tolerance_ticks",), ("liquidity_max_pools",), ("fvg_visible",),
                         ("order_blocks_visible",), ("chart_theme",)):
                chunk = {k: data[k] for k in part if k in data}
                try:
                    merged = parse_update(chunk, merged)
                except ValueError as exc:
                    logger.warning("Ignoring invalid %s in %s (%s).", "/".join(part), self.path, exc)
            if data.get("preset") == CUSTOM:
                merged = replace(merged, preset=CUSTOM)
            # Files without `colors_chosen` predate automatic theme palettes. Known untouched defaults migrate to the current default;
            # any other colours represent a user choice and are kept.
            if "colors_chosen" in data:
                chosen = data["colors_chosen"] is True
                if not chosen:
                    bull, bear = _preset_colors(DEFAULT_PRESET, merged.chart_theme if merged.chart_theme in THEMES else merged.theme)
                    merged = replace(merged, preset=DEFAULT_PRESET, bull_color=bull, bear_color=bear)
            elif (merged.bull_color, merged.bear_color) in (LEGACY_DEFAULT_COLORS, PREVIOUS_DEFAULT_COLORS):
                chosen = False
                bull, bear = _preset_colors(DEFAULT_PRESET, merged.chart_theme if merged.chart_theme in THEMES else merged.theme)
                merged = replace(merged, preset=DEFAULT_PRESET, bull_color=bull, bear_color=bear)
            else:
                chosen = (merged.bull_color, merged.bear_color) != _preset_colors(
                    DEFAULT_PRESET, merged.chart_theme if merged.chart_theme in THEMES else merged.theme)
            return replace(merged, colors_chosen=chosen)
        except FileNotFoundError:
            return ChartSettings()
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable %s (%s); using the default chart settings.", self.path, exc)
            return ChartSettings()

    def save(self, settings: ChartSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, data: object) -> ChartSettings:
        """Validate first, then save. A bad request changes nothing."""
        new = parse_update(data, self.load())
        self.save(new)
        return new
