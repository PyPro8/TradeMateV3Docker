/* TRADING JOURNAL: the user's personal notepad (date, time, instrument, strategy, result, profit/loss, emotion, notes).
   It documents trades; it never trades. Entries are stored by the server (/api/trade-journal). Text is always written with textContent. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  if (!$('jr-panel')) return;
  var editing = null, entries = [], emotionsLoaded = false;

  function msg(t, bad) { var m = $('jr-msg'); m.textContent = t || ''; m.className = 'note' + (bad ? ' is-error' : ''); }
  function api(method, url, body) {
    return fetch(url, { method: method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store' })
      .then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Unexpected answer from TradeMate.' }; }); })
      .catch(function () { return { ok: false, message: 'TradeMate could not be reached.' }; });
  }
  function title(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

  function formValues() {
    var pl = $('jr-pl').value.trim();
    return { date: $('jr-date').value, time: $('jr-time').value, instrument: $('jr-instrument').value, strategy: $('jr-strategy').value, result: $('jr-result').value,
             profit_loss: pl === '' ? null : Number(pl), emotion: $('jr-emotion').value, notes: $('jr-notes').value };
  }
  function fill(e) {
    $('jr-date').value = e ? e.date || '' : ''; $('jr-time').value = e ? e.time || '' : ''; $('jr-instrument').value = e ? e.instrument || '' : '';
    $('jr-strategy').value = e ? e.strategy || '' : ''; $('jr-result').value = e ? e.result || '' : '';
    $('jr-pl').value = e && e.profit_loss != null ? e.profit_loss : ''; $('jr-emotion').value = e ? e.emotion || '' : ''; $('jr-notes').value = e ? e.notes || '' : '';
    editing = e ? e.id : null;
    $('jr-save').textContent = e ? 'UPDATE ENTRY' : 'SAVE ENTRY';
  }

  function render() {
    var body = $('jr-body'); body.replaceChildren();
    $('jr-count').textContent = String(entries.length);
    if (!entries.length) { var tr = document.createElement('tr'); tr.className = 'empty-row'; var td = document.createElement('td'); td.colSpan = 9; td.textContent = 'No journal entries yet'; tr.appendChild(td); body.appendChild(tr); return; }
    entries.forEach(function (e) {
      var tr = document.createElement('tr'); tr.setAttribute('data-id', e.id);
      var a = e.auto || {}, note = e.notes || '';
      if (a.source === 'rapid_fire') note = '[Rapid Fire #' + a.ticket + ' ' + String(a.side || '').toUpperCase() + ' @ ' + a.entry_price + (a.exit_price != null ? ' to ' + a.exit_price : '') + (a.closed_by ? ', closed by ' + a.closed_by : '') + '] ' + note;
      [e.date, e.time, e.instrument, e.strategy, title(e.result), e.profit_loss == null ? '' : Number(e.profit_loss).toFixed(2), title(e.emotion), note].forEach(function (v, i) {
        var c = document.createElement('td'); if (i === 5) c.className = 'num'; if (i === 7) c.className = 'jr-note'; c.textContent = v == null ? '' : String(v); tr.appendChild(c);
      });
      var act = document.createElement('td');
      [['Edit', function () { fill(e); msg('Editing this entry. Your changes are saved when you press UPDATE ENTRY.'); window.scrollTo(0, 0); }],
       ['Delete', function () { if (!window.confirm('Delete this journal entry?')) return; api('DELETE', '/api/trade-journal/' + encodeURIComponent(e.id)).then(function (j) { msg(j.ok ? 'Entry deleted.' : (j.message || 'Not deleted.'), !j.ok); if (j.ok) { if (editing === e.id) fill(null); load(); } }); }]]
        .forEach(function (b) { var btn = document.createElement('button'); btn.type = 'button'; btn.className = 'btn btn-sm'; btn.textContent = b[0]; btn.addEventListener('click', b[1]); act.appendChild(btn); });
      tr.appendChild(act); body.appendChild(tr);
    });
  }

  function load() {
    return api('GET', '/api/trade-journal').then(function (j) {
      if (!j || !j.ok) { msg((j && j.message) || 'The journal could not be loaded.', true); return; }
      if (!emotionsLoaded) { var sel = $('jr-emotion'); (j.emotions || []).forEach(function (m) { if (!m) return; var o = document.createElement('option'); o.value = m; o.textContent = title(m); sel.appendChild(o); }); emotionsLoaded = true; }
      entries = j.entries || []; render();
    });
  }

  $('jr-save').addEventListener('click', function () {
    var body = formValues();
    var req = editing ? api('PUT', '/api/trade-journal/' + encodeURIComponent(editing), body) : api('POST', '/api/trade-journal', body);
    req.then(function (j) {
      if (!j.ok) { msg((j.problems && j.problems.join(' ')) || j.message || 'Not saved.', true); return; }     // a refused save keeps everything that was typed
      msg(editing ? 'Entry updated.' : 'Entry saved.'); fill(null); load();
    });
  });
  $('jr-clear').addEventListener('click', function () { fill(null); msg(''); });
  $('jr-import').addEventListener('click', function () { api('POST', '/api/trade-journal/import-rapid-fire', {}).then(function (j) { msg(j.message || 'Done.', !j.ok); load(); }); });

  window.addEventListener('hashchange', function () { if (/journal/i.test(location.hash)) load(); });
  load();
  window.TradeMateJournal = { load: load, entries: function () { return entries.slice(); } };
})();
