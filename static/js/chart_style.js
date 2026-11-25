/* TradeMate: pure helpers for candle colours and market-structure overlay lookups (Phase 7A).

   No DOM, no network, so Node can test them (tests/chart_style_test.js).

   Candle colour semantics: close > open = bullish, close < open = bearish, close == open = DOJI. A doji is neither
   bullish nor bearish: it is drawn in the theme's neutral axis colour, never in a bullish / bearish colour.
   Only the way a candle is DRAWN is decided here; the OHLC numbers are never modified. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateChartStyle = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var HEX = /^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$/;
  // The chart canvas background in each theme (see style.css), used only to WARN about low contrast.
  var CHART_BG = { dark: '#060B17', light: '#F3F6FC' };
  var LOW_CONTRAST = 3;        // WCAG ratio for graphical objects

  /* '#abc' -> '#AABBCC'; null when it is not a valid #RGB / #RRGGBB colour. */
  function normalizeHex(value) {
    if (typeof value !== 'string' || !HEX.test(value.trim())) return null;
    var t = value.trim().toUpperCase();
    if (t.length === 4) t = '#' + t[1] + t[1] + t[2] + t[2] + t[3] + t[3];
    return t;
  }

  /* 'bull' | 'bear' | 'doji' from the candle's own open and close. */
  function candleKind(open, close) {
    return close > open ? 'bull' : close < open ? 'bear' : 'doji';
  }

  function channel(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
  function luminance(hex) {
    var h = normalizeHex(hex); if (!h) return null;
    return 0.2126 * channel(parseInt(h.slice(1, 3), 16)) + 0.7152 * channel(parseInt(h.slice(3, 5), 16)) + 0.0722 * channel(parseInt(h.slice(5, 7), 16));
  }
  function contrastRatio(a, b) {
    var la = luminance(a), lb = luminance(b);
    if (la == null || lb == null) return null;
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  /* True when a candle colour is hard to see on this theme's background. Used to add a thin outline round the BODY
     (the fill colour is never changed) and to show the warning. */
  function isLowContrast(color, theme) {
    var r = contrastRatio(color, CHART_BG[theme] || CHART_BG.dark);
    return r != null && r < LOW_CONTRAST;
  }

  /* Warning text for the settings panel, or '' when both colours are fine on the active theme. */
  function contrastWarning(bull, bear, theme) {
    var bad = [];
    if (isLowContrast(bull, theme)) bad.push('bullish');
    if (isLowContrast(bear, theme)) bad.push('bearish');
    if (!bad.length) return '';
    return 'Low contrast on the ' + (theme === 'light' ? 'light' : 'dark') + ' chart background for the ' + bad.join(' and ') +
      ' candles. Your colours are kept; a thin outline is added so the candles stay visible.';
  }

  /* Preset id whose two colours match exactly, else 'custom'. `presets` = [{id, bull_color, bear_color}]. */
  function matchPreset(bull, bear, presets) {
    var b = normalizeHex(bull), r = normalizeHex(bear);
    for (var i = 0; i < (presets || []).length; i++) {
      var p = presets[i], colors = [p];
      if (p.theme_colors) Object.keys(p.theme_colors).forEach(function (theme) { colors.push(p.theme_colors[theme]); });
      for (var j = 0; j < colors.length; j++) {
        if (normalizeHex(colors[j].bull_color) === b && normalizeHex(colors[j].bear_color) === r) return p.id;
      }
    }
    return 'custom';
  }

  /* Fill / wick colour of one candle: bullish colour, bearish colour, or the neutral colour for a doji. */
  function candleColor(kind, style, neutral) {
    return kind === 'bull' ? style.bull : kind === 'bear' ? style.bear : neutral;
  }

  /* time -> index lookup for the structure overlay. Overlay items whose time is not among the loaded candles are
     simply not drawn: nothing is ever placed at an invented position. */
  function indexByTime(candles) {
    var map = Object.create(null);
    for (var i = 0; i < (candles || []).length; i++) map[candles[i][0]] = i;
    return map;
  }

  /* Flat list of drawable structure items for the visible candles. `structure` is the /api/candles payload part. */
  function overlayItems(structure, candles) {
    var out = { swings: [], events: [] };
    if (!structure || !candles || !candles.length) return out;
    var idx = indexByTime(candles);
    (structure.swings || []).forEach(function (s) {
      var i = idx[s.pivot_time];
      if (i !== undefined && s.classification) out.swings.push({ id: s.id, index: i, kind: s.kind, price: s.price, label: s.classification, confirmed: s.confirmed });
    });
    (structure.events || []).forEach(function (e) {
      var from = idx[e.reference_pivot_time], to = idx[e.candle_time];
      if (from !== undefined && to !== undefined) out.events.push({ id: e.id, from: from, to: to, type: e.type, direction: e.direction, level: e.level, confirmed: e.confirmed });
    });
    return out;
  }

  return { normalizeHex: normalizeHex, candleKind: candleKind, contrastRatio: contrastRatio, isLowContrast: isLowContrast,
           contrastWarning: contrastWarning, matchPreset: matchPreset, candleColor: candleColor, indexByTime: indexByTime,
           overlayItems: overlayItems, CHART_BG: CHART_BG };
});
