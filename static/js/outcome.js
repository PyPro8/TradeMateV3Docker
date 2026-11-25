/* TradeMate: turns a write response (/api/order, /api/position/<t>/modify|close) into the text the user sees.

   Branches ONLY on the structured fields the server sends (`outcome`, `order_submitted`, `request`, `risk`, `broker`),
   never on message wording. A success look is given to `executed` and nothing else: a rejection or an uncertain
   result can never be shown as green. Pure function, no DOM: tested by tests/outcome_test.js. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateOutcome = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TITLES = {
    executed: 'EXECUTED',
    risk_rejected: 'TRADEMATE RISK REJECTED',
    execution_refused: 'EXECUTION ENGINE REFUSED',
    invalid_request: 'INVALID REQUEST',
    connection_failed: 'MT5 CONNECTION FAILURE',
    broker_rejected: 'MT5/BROKER REJECTED',
    accepted_unconfirmed: 'ACCEPTED, NOT CONFIRMED YET',
    unknown: 'EXECUTION OUTCOME UNKNOWN',
    not_executed: 'NOT EXECUTED (RECONCILED)',
    internal_error: 'TRADEMATE INTERNAL ERROR'
  };
  var KIND = { executed: 'ok', accepted_unconfirmed: 'unknown', unknown: 'unknown' };   // everything else is a failure

  function has(v) { return v !== null && v !== undefined && v !== ''; }
  function money(v, cur) { return typeof v === 'number' && isFinite(v) ? v.toFixed(2) + (cur ? ' ' + cur : '') : null; }

  function requestLines(r) {
    var out = [];
    if (!r) return out;
    if (has(r.symbol)) out.push('Symbol: ' + r.symbol);
    if (has(r.side)) out.push('Direction: ' + String(r.side).toUpperCase());
    if (has(r.ticket)) out.push('Position: #' + r.ticket + (has(r.action) ? ' (' + r.action + ')' : ''));
    if (has(r.volume)) out.push('Volume: ' + r.volume);
    if (has(r.sl)) out.push('Stop-loss: ' + r.sl);
    if (has(r.tp)) out.push('Take-profit: ' + r.tp);
    return out;
  }

  function submittedLine(res) {
    if (res.order_submitted === true) return 'Order submitted to MT5: YES';
    if (res.order_submitted === false) return 'Order submitted to MT5: NO';
    return 'Order submitted to MT5: UNKNOWN';
  }

  /* -> { kind: 'ok'|'fail'|'unknown', title, text }.  `res` may be null/legacy (no `outcome`): then only `state` is used. */
  function describe(res) {
    res = res || {};
    var outcome = res.outcome;
    if (!outcome) {                                    // older / network-level answers: fall back to the coarse state
      outcome = res.state === 'executed' ? 'executed' : res.state === 'unconfirmed' ? 'accepted_unconfirmed' :
                res.state === 'unknown' ? 'unknown' : res.state === 'rejected_risk' ? 'risk_rejected' : 'execution_refused';
    }
    var title = TITLES[outcome] || 'FAILED';
    var kind = KIND[outcome] || 'fail';
    var lines = requestLines(res.request);
    if (outcome === 'executed') {
      var tail = has(res.message) ? String(res.message).replace(/^Executed:?\s*/, '') : '';
      return { kind: 'ok', title: title, text: [tail].concat(lines).filter(Boolean).join('\n') };
    }
    if (has(res.message)) lines.push('Reason: ' + res.message);
    if (outcome === 'risk_rejected' && res.risk) {
      var cur = res.risk.currency;
      var req = money(res.risk.requested_risk_amount, cur), max = money(res.risk.max_risk_amount, cur);
      if (req) lines.push('Requested risk: ' + req + (typeof res.risk.requested_risk_percent === 'number' ? ' (' + res.risk.requested_risk_percent.toFixed(2) + '%)' : ''));
      if (max) lines.push('Maximum permitted risk: ' + max + (typeof res.risk.max_risk_percent === 'number' ? ' (' + res.risk.max_risk_percent.toFixed(2) + '%)' : ''));
    }
    if (outcome === 'broker_rejected' && res.broker) {
      if (has(res.broker.retcode)) lines.push('Retcode: ' + res.broker.retcode);
      if (has(res.broker.message)) lines.push('Broker explanation: ' + res.broker.message);
    }
    lines.push(submittedLine(res));
    if (outcome === 'unknown') {
      lines.push('Automatic resubmission: DISABLED');
      lines.push('Next: TradeMate checks MT5 positions and history. Do NOT resend this order yourself until it is resolved.');
    }
    if (outcome === 'accepted_unconfirmed') lines.push('The position is not confirmed in the account yet. Do not resend.');
    return { kind: kind, title: title, text: lines.join('\n') };
  }

  return { describe: describe, TITLES: TITLES };
});
