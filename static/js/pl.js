/* TradeMate: price move -> pips and account-currency money, for the TP/SL lines.

   No hard-coded pip values. Money comes from the instrument's own tick data as MT5 reports it:

       money = (favourable price move / tick_size) * tick_value * lots

   MT5 expresses tick_value in the ACCOUNT currency already (it applies the broker's conversion rates), and
   reports a separate value for a gain (tick_value_profit) and a loss (tick_value_loss) because on cross-currency
   instruments they differ. We use the matching one and fall back to plain tick_value (older bridges).

   If any input is missing the result is null and the UI shows a dash. It never guesses.
   Not included: swap, commission and spread/slippage at the moment of closing. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMatePL = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }

  /* 1 pip = 10 points on 3/5-digit symbols (JPY pairs, most forex), otherwise 1 point.
     That is the usual MT5 convention; for metals/indices/crypto "pip" is just the quote's smallest step. */
  function pipSize(spec) {
    var point = spec && num(spec.point), digits = spec && num(spec.digits);
    if (!point || point <= 0) return null;
    return (digits === 3 || digits === 5) ? point * 10 : point;
  }

  /* Price move in the trade's favour: positive = profit, negative = loss. */
  function favourableMove(direction, entry, target) {
    entry = num(entry); target = num(target);
    if (entry == null || target == null) return null;
    if (direction === 'buy') return target - entry;
    if (direction === 'sell') return entry - target;
    return null;
  }

  function pips(spec, direction, entry, target) {
    var move = favourableMove(direction, entry, target), size = pipSize(spec);
    return move == null || !size ? null : move / size;
  }

  function money(spec, direction, entry, target, volume) {
    var move = favourableMove(direction, entry, target);
    volume = num(volume);
    if (move == null || volume == null || volume <= 0 || !spec) return null;
    var tickSize = num(spec.tick_size);
    if (!tickSize || tickSize <= 0) return null;
    var perTick = move >= 0 ? (num(spec.tick_value_profit) || num(spec.tick_value))
                            : (num(spec.tick_value_loss) || num(spec.tick_value));
    if (!perTick || perTick <= 0) return null;
    return (move / tickSize) * perTick * volume;
  }

  /* A level typed/dragged BEFORE an order exists has no side yet. Infer the one that makes it sensible:
     TP above the market or SL below it -> BUY; TP below or SL above -> SELL. */
  function inferSide(field, level, bid, ask) {
    level = num(level); bid = num(bid); ask = num(ask);
    if (level == null || bid == null || ask == null) return null;
    var mid = (bid + ask) / 2;
    return (field === 'tp') === (level > mid) ? 'buy' : 'sell';
  }

  /* An order opens at the ask when buying and at the bid when selling. */
  function entryFor(side, bid, ask) { return side === 'buy' ? num(ask) : side === 'sell' ? num(bid) : null; }

  function fmtPips(v) {
    if (v == null) return '\u2014';
    var r = Math.round(v * 10) / 10;
    return (r > 0 ? '+' : r < 0 ? '\u2212' : '') + Math.abs(r).toFixed(1) + ' pips';
  }

  function fmtMoney(v, currency) {
    if (v == null) return '\u2014';
    var r = Math.round(v * 100) / 100, sign = r > 0 ? '+' : r < 0 ? '\u2212' : '', abs = Math.abs(r), body;
    try {
      body = new Intl.NumberFormat(undefined, { style: 'currency', currency: currency, currencyDisplay: 'narrowSymbol' }).format(abs);
    } catch (e) {   // missing or non-ISO currency code (e.g. a cent account): show the plain number + code
      body = abs.toFixed(2) + (currency ? ' ' + currency : '');
    }
    return sign + body;
  }

  /* Live P/L of an OPEN position. MT5's own floating profit is the source of truth: it already follows the broker's
     symbol mechanics. Only if MT5 sent none do we derive it from the position's direction, open price, the current
     closing price (bid for a BUY, ask for a SELL, as MT5 reports in price_current) and the symbol's tick data.
     BUY gains when price rises, SELL gains when it falls: favourableMove() is direction-aware. */
  function positionPL(p, spec) {
    if (!p) return { value: null, source: null };
    if (num(p.profit) != null) return { value: p.profit, source: 'mt5' };
    var v = money(spec, p.direction, p.price_open, p.price_current, p.volume);
    return { value: v, source: v == null ? null : 'computed' };
  }

  /* Text for the entry line, e.g. "BUY +25.40 USD" / "SELL -9.45 USD". `plText` is the UI's own signed money formatter. */
  function entryLabel(direction, plText) {
    return (direction === 'buy' ? 'BUY ' : direction === 'sell' ? 'SELL ' : '') + plText;
  }

  return { positionPL: positionPL, entryLabel: entryLabel, pipSize: pipSize, favourableMove: favourableMove, pips: pips, money: money, inferSide: inferSide,
           entryFor: entryFor, fmtPips: fmtPips, fmtMoney: fmtMoney };
});
