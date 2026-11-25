/* TradeMate Command Centre (foundation). Shows the REAL state of every Command Centre capability from /api/strategylab/status.
   It decides nothing itself: a control looks operational only when the backend says its capability is Available.
   Server text is placed with textContent only (no HTML is built from it). */
(function () {
  'use strict';
  var view = document.getElementById('view-strategylab');
  if (!view) return;
  var REFRESH_MS = 10000, timer = null, seq = 0, greeter = null;
  var searchSeq = 0, searchTimer = null, searchController = null, analysisController = null, analysing = false;
  var greetingEl = document.getElementById('lab-greeting');
  var displayNameInput = document.getElementById('display-name-input');
  var displayNameSave = document.getElementById('display-name-save');
  var displayNameClear = document.getElementById('display-name-clear');
  var displayNameStatus = document.getElementById('display-name-status');
  var storage = null;
  try { storage = window.localStorage; } catch (error) {
    if (displayNameStatus) displayNameStatus.textContent = 'Browser storage is unavailable; preferences will not persist.';
  }
  if (greetingEl && window.TradeMateLabGreeting) greeter = window.TradeMateLabGreeting.mount(greetingEl, { storage: storage });   // reads the computer's local clock only

  function renderDisplayNameField() {
    if (!displayNameInput || !window.TradeMateLabGreeting) return;
    var name = window.TradeMateLabGreeting.readDisplayName(storage);
    displayNameInput.value = name;
  }

  function setDisplayNameStatus(message, isError) {
    if (!displayNameStatus) return;
    displayNameStatus.textContent = message;
    displayNameStatus.classList.toggle('is-error', !!isError);
  }

  function saveDisplayName() {
    if (!displayNameInput || !window.TradeMateLabGreeting) return;
    var value = displayNameInput.value;
    var normalized = window.TradeMateLabGreeting.sanitizeDisplayName(value);
    if (value.trim() && (!normalized || normalized.length > 32 || normalized !== value.replace(/\s+/g, ' ').trim())) {
      displayNameInput.setAttribute('aria-invalid', 'true');
      setDisplayNameStatus('Use a name up to 32 characters with letters, numbers, spaces, apostrophes, periods or hyphens.', true);
      return;
    }
    var cleaned = window.TradeMateLabGreeting.saveDisplayName(storage, value);
    if (window.TradeMateLabGreeting.readDisplayName(storage) !== (cleaned || '')) {
      displayNameInput.setAttribute('aria-invalid', 'true');
      setDisplayNameStatus('Name could not be saved on this browser.', true);
      return;
    }
    displayNameInput.removeAttribute('aria-invalid');
    if (greeter) greeter.render();
    renderDisplayNameField();
    setDisplayNameStatus(cleaned ? 'Saved your preferred name.' : 'Cleared the preferred name.');
  }

  function clearDisplayName() {
    if (!displayNameInput || !window.TradeMateLabGreeting) return;
    displayNameInput.value = '';
    window.TradeMateLabGreeting.saveDisplayName(storage, '');
    if (window.TradeMateLabGreeting.readDisplayName(storage)) {
      displayNameInput.setAttribute('aria-invalid', 'true');
      setDisplayNameStatus('Preferred name could not be cleared on this browser.', true);
      return;
    }
    displayNameInput.removeAttribute('aria-invalid');
    if (greeter) greeter.render();
    setDisplayNameStatus('Cleared the preferred name.');
  }

  if (displayNameInput) {
    renderDisplayNameField();
    displayNameInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { saveDisplayName(); }
    });
  }
  if (displayNameSave) displayNameSave.addEventListener('click', saveDisplayName);
  if (displayNameClear) displayNameClear.addEventListener('click', clearDisplayName);
  if (displayNameInput) displayNameInput.addEventListener('input', function () {
    displayNameInput.removeAttribute('aria-invalid');
    setDisplayNameStatus('', false);
  });

  function states() { return view.querySelectorAll('[data-cap]'); }
  function setAnalysisBusy(busy) {
    analysing = busy;
    var button = document.getElementById('lab-analyse');
    var input = document.getElementById('lab-market');
    var dropdown = document.getElementById('lab-market-dropdown');
    // Keep analysis controls interactive even when broker status is unavailable.
    // The API remains authoritative and will return the precise connection/data error;
    // disabling the whole workflow here hides that diagnostic behind a prohibited cursor.
    if (button) { button.disabled = busy; button.classList.toggle('is-busy', busy); }
    var label = document.getElementById('lab-analyse-text'); if (label) label.textContent = busy ? 'ANALYSING…' : 'ANALYSE MARKET';
    if (input) input.disabled = false;
    if (dropdown) dropdown.disabled = false;
  }
  function setAll(state, text) {
    Array.prototype.forEach.call(states(), function (n) { n.setAttribute('data-state', state); n.textContent = text; });
  }
  function node(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }
  var notificationEnabled = false, soundEnabled = false, soundVolume = 0.4, lastEventId = null, seenEventKeys = [], audioContext = null;
  var notificationToggle = document.getElementById('lab-notification-enabled');
  var notificationPermissionButton = document.getElementById('lab-notification-permission');
  var soundToggle = document.getElementById('lab-sound-enabled');
  var soundVolumeInput = document.getElementById('lab-sound-volume');
  var alertNote = document.getElementById('lab-alert-note');
  var watchdogState = null;

  function alertMessage(message) {
    if (alertNote) alertNote.textContent = message;
  }
  function readStored(key, fallback) {
    try {
      if (!storage) return fallback;
      var value = storage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch (error) {
      alertMessage('Browser storage could not be read; alert preferences may not persist.');
      return fallback;
    }
  }
  function writeStored(key, value) {
    try {
      if (!storage) throw new Error('Browser storage is unavailable.');
      storage.setItem(key, JSON.stringify(value));
      return true;
    } catch (error) {
      alertMessage('Browser storage could not save this preference.');
      return false;
    }
  }
  function initializeAlertControls() {
    notificationEnabled = readStored('trademate.lab.notifications.enabled', false) === true;
    soundEnabled = readStored('trademate.lab.sound.enabled', false) === true;
    var volume = readStored('trademate.lab.sound.volume', 0.4);
    soundVolume = typeof volume === 'number' && isFinite(volume) ? Math.max(0, Math.min(1, volume)) : 0.4;
    lastEventId = readStored('trademate.lab.watchdog.lastEventId', null);
    if (typeof lastEventId !== 'number' || !isFinite(lastEventId) || lastEventId < 0) lastEventId = null;
    seenEventKeys = readStored('trademate.lab.notifications.seen', []);
    if (!Array.isArray(seenEventKeys)) seenEventKeys = [];
    if (notificationToggle) notificationToggle.checked = notificationEnabled;
    if (soundToggle) soundToggle.checked = soundEnabled;
    if (soundVolumeInput) soundVolumeInput.value = String(soundVolume);
    refreshAlertControls();
  }
  function refreshAlertControls() {
    var supported = typeof window.Notification !== 'undefined';
    if (notificationPermissionButton) notificationPermissionButton.disabled = !supported;
    if (notificationToggle) {
      notificationToggle.disabled = !supported || window.Notification.permission !== 'granted';
      notificationToggle.checked = notificationEnabled && !notificationToggle.disabled;
    }
    if (soundToggle) soundToggle.disabled = typeof window.AudioContext === 'undefined';
    if (!supported) alertMessage('This browser does not support desktop notifications. In-page monitoring status remains available.');
    else if (window.Notification.permission === 'denied') alertMessage('Browser notifications are blocked in site settings. You can still use in-page monitoring and sound.');
    else if (window.Notification.permission !== 'granted') alertMessage('Use “Enable browser alerts” to grant permission, then opt in to desktop alerts.');
    else if (notificationEnabled) alertMessage('Desktop alerts are enabled for newly detected, deduplicated CHoCH events and qualified setups.');
    else alertMessage('Permission is granted; desktop alerts remain off until you opt in.');
  }
  function createAudioContext() {
    if (typeof window.AudioContext === 'undefined') return null;
    try {
      if (!audioContext) audioContext = new window.AudioContext();
      if (audioContext.state === 'suspended') audioContext.resume().catch(function () {
        alertMessage('The browser did not allow sound playback. Interact with the page and try again.');
      });
      return audioContext;
    } catch (error) {
      alertMessage('Sound could not be started in this browser.');
      return null;
    }
  }
  function playSetupSound() {
    var context = createAudioContext();
    if (!context || soundVolume <= 0) return;
    try {
      var oscillator = context.createOscillator(), gain = context.createGain();
      oscillator.type = 'sine';
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(Math.max(0.0001, soundVolume * 0.08), context.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.2);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.21);
    } catch (error) {
      alertMessage('The browser could not play the alert sound. Interact with the page and enable sound again.');
    }
  }
  function watchdogEventTime(timestamp, nowMs) {
    var t = Number(timestamp);
    if (!isFinite(t) || t <= 0) return 'Event time unavailable';
    var date = new Date(t * 1000);
    if (!isFinite(date.getTime())) return 'Event time unavailable';
    var seconds = Math.max(0, (nowMs - date.getTime()) / 1000);
    var age = seconds < 1 ? 'less than 1 second ago' : seconds < 60 ? Math.floor(seconds) + ' seconds ago' :
      seconds < 3600 ? Math.floor(seconds / 60) + ' minutes ago' : seconds < 86400 ? Math.floor(seconds / 3600) + ' hours ago' :
      Math.floor(seconds / 86400) + ' days ago';
    return date.toLocaleString() + ' · ' + age;
  }
  function notifyWatchdogEvent(event) {
    var isChoch = event.event_type === 'choch';
    var when = watchdogEventTime(event.timestamp, Date.now());
    var title = isChoch
      ? 'TradeMate: ' + event.direction + ' CHoCH · ' + event.symbol + ' · ' + event.timeframe
      : 'TradeMate: ' + event.decision + ' setup · ' + event.symbol;
    var details = isChoch
      ? event.summary || ('Confirmed ' + event.direction + ' Change of Character at ' + event.level + '.')
      : 'Entry ' + event.entry + ' · Stop ' + event.stop_loss + ' · Target ' + event.take_profit_1;
    var body = when + '\n' + details;
    if (notificationEnabled && typeof window.Notification !== 'undefined' && window.Notification.permission === 'granted') {
      try {
        new window.Notification(title, { body: body, tag: event.event_key });
      } catch (error) {
        alertMessage('The browser could not display this desktop alert.');
      }
    }
    if (soundEnabled) playSetupSound();
    if (isChoch) alertMessage('CHoCH detected: ' + event.direction + ' on ' + event.symbol + ' (' + event.timeframe + ').');
  }
  function handleWatchdogEvents(monitor) {
    if (!monitor || !Array.isArray(monitor.events)) return;
    // Server event IDs are process-local and can reset after a restart. Event keys
    // are the durable deduplication identity; do not mark every first-poll event as
    // seen, because that silently discards a setup created just before the poll.
    if (typeof monitor.latest_event_id === 'number' && typeof lastEventId === 'number' && monitor.latest_event_id < lastEventId) {
      lastEventId = 0;
    }
    monitor.events.forEach(function (event) {
      if (typeof event.id !== 'number') return;
      var isNewId = lastEventId === null || event.id > lastEventId;
      if (isNewId && event.event_key && seenEventKeys.indexOf(event.event_key) === -1) {
        seenEventKeys.push(event.event_key);
        seenEventKeys = seenEventKeys.slice(-100);
        notifyWatchdogEvent(event);
      }
      if (lastEventId === null || event.id > lastEventId) lastEventId = event.id;
    });
    if (typeof monitor.latest_event_id === 'number' && (lastEventId === null || monitor.latest_event_id > lastEventId)) {
      lastEventId = monitor.latest_event_id;
    }
    writeStored('trademate.lab.watchdog.lastEventId', lastEventId === null ? 0 : lastEventId);
    writeStored('trademate.lab.notifications.seen', seenEventKeys);
  }
  function renderWatchdog(monitor) {
    watchdogState = monitor || null;
    var checkbox = document.getElementById('lab-watchdog');
    if (checkbox && monitor) {
      checkbox.checked = monitor.active === true;
      // The checkbox starts/stops monitoring. Do not gate it on the market-data
      // badge: that badge can be unavailable while the broker is connected, and
      // the server start endpoint performs the authoritative symbol/data checks.
      var brokerState = document.querySelector('[data-cap="broker"]');
      var brokerConnected = !!(brokerState && brokerState.getAttribute('data-state') === 'Available');
      checkbox.disabled = !monitor.active && !brokerConnected;
    }
    var note = document.getElementById('lab-watchdog-note');
    if (note && monitor) {
      var tracked = Array.isArray(monitor.candidate_states) ? monitor.candidate_states : [];
      var candidateSummary = tracked.length ? ' · ' + tracked.length + ' tracked candidate(s): ' +
        tracked.slice(0, 3).map(function (candidate) {
          return candidate.setup_family.replace(/_/g, ' ') + ' ' + candidate.direction + ' (' + candidate.state.replace(/_/g, ' ') + ')';
        }).join('; ') : '';
      note.textContent = (monitor.message || 'Watchdog status unavailable.') + candidateSummary;
    }
    renderWatchdogLabel(monitor);
    var last = document.getElementById('lab-watchdog-last');
    if (last) {
      if (monitor && typeof monitor.last_checked === 'number') {
        last.textContent = 'Monitoring ' + (monitor.symbol || 'selected market') + ' · last check ' + new Date(monitor.last_checked * 1000).toLocaleString() +
          (monitor.last_decision ? ' · ' + monitor.last_decision : '');
      } else last.textContent = 'No monitoring check has run yet.';
    }
  }
  function requestWatchdog(active) {
    var checkbox = document.getElementById('lab-watchdog');
    if (!checkbox) return;
    checkbox.disabled = true;
    var endpoint = active ? '/api/strategylab/watchdog/start' : '/api/strategylab/watchdog/stop';
    var options = { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' } };
    if (active) options.body = JSON.stringify({ symbol: document.getElementById('lab-market').value.trim() });
    fetch(endpoint, options).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok || !body.ok) throw new Error(body.message || 'Watchdog request failed.');
        document.getElementById('lab-watchdog-note').textContent = body.message;
        return load();
      });
    }).catch(function (error) {
      checkbox.checked = !active;
      document.getElementById('lab-watchdog-note').textContent = error.message || 'Watchdog request failed. Check the broker connection.';
      checkbox.disabled = false;
    });
  }
  if (notificationPermissionButton) notificationPermissionButton.addEventListener('click', function () {
    if (typeof window.Notification === 'undefined') { refreshAlertControls(); return; }
    window.Notification.requestPermission().then(function (permission) {
      if (permission === 'granted') {
        notificationEnabled = true;
        writeStored('trademate.lab.notifications.enabled', true);
      }
      refreshAlertControls();
    }).catch(function () { alertMessage('Notification permission could not be requested. Check browser site settings.'); });
  });
  if (notificationToggle) notificationToggle.addEventListener('change', function () {
    notificationEnabled = notificationToggle.checked;
    writeStored('trademate.lab.notifications.enabled', notificationEnabled);
    refreshAlertControls();
  });
  if (soundToggle) soundToggle.addEventListener('change', function () {
    soundEnabled = soundToggle.checked;
    if (soundEnabled) createAudioContext();
    writeStored('trademate.lab.sound.enabled', soundEnabled);
    refreshAlertControls();
  });
  if (soundVolumeInput) soundVolumeInput.addEventListener('input', function () {
    soundVolume = Math.max(0, Math.min(1, Number(soundVolumeInput.value) || 0));
    writeStored('trademate.lab.sound.volume', soundVolume);
  });
  initializeAlertControls();
  function row(parent, label, value) {
    var item = node('div', 'lab-result-row');
    item.appendChild(node('span', 'lab-result-label', label));
    item.appendChild(node('span', 'lab-result-value', value));
    parent.appendChild(item);
  }
  // ---------------------------------------------------------------------------------------------------------------------------
  // Strategy-aware analysis display. Every word shown here is taken from the backend result; nothing is invented or defaulted to a direction.
  // ---------------------------------------------------------------------------------------------------------------------------
  var STAGE_TITLES = { context: 'Market context', setup_candidate: 'Setup candidate', confirmation: 'Confirmation', trade_levels: 'Trade levels' };
  var STAGE_WORDS = { met: 'Met', not_met: 'Not met', unknown: 'Unknown', not_evaluated: 'Not evaluated' };
  var lastResult = null, lastSymbol = '', lastEpoch = null, catalogue = null, approval = null, countdownTimer = null, armTimer = null;
  var reviewBusy = false, executeBusy = false;

  function setText(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }
  function show(id, on) { var el = document.getElementById(id); if (el) el.hidden = !on; }

  function resetReview() {
    approval = null;
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
    reviewBusy = false; executeBusy = false;
    var res = document.getElementById('cc-review-result'); if (res) clear(res);
    show('cc-execute', false);
    var ex = document.getElementById('cc-execute-btn');
    if (ex) { ex.removeAttribute('data-armed'); ex.textContent = 'CONFIRM & EXECUTE'; ex.disabled = false; }
    show('cc-cancel-btn', false);
    setText('cc-execute-note', '');
    setText('cc-exec-state', lastResult && (lastResult.decision === 'BUY' || lastResult.decision === 'SELL') ? 'A qualified setup can be reviewed.' : 'Nothing under review.');
    var rb = document.getElementById('cc-review-btn');
    if (rb) rb.disabled = !(lastResult && (lastResult.decision === 'BUY' || lastResult.decision === 'SELL'));
  }

  function clearAnalysis(message) {
    lastResult = null;
    ['lab-decision-body', 'lab-mtf-body', 'lab-setup-body'].forEach(function (id) {
      var el = document.getElementById(id); clear(el);
      if (id === 'lab-decision-body') {
        var empty = node('div', 'lab-empty'); empty.id = 'lab-decision-empty';
        empty.appendChild(node('p', 'lab-empty-title', message ? 'No result' : 'No analysis yet'));
        empty.appendChild(node('p', 'note', message || 'Pick a market and a strategy, then press ANALYSE MARKET.'));
        el.appendChild(empty);
      } else el.appendChild(node('p', 'note', message || 'Run ANALYSE MARKET to fill this in from the selected strategy.'));
    });
    show('cc-review', false);
    setText('cc-decision-meta', '');
    resetReview();
    setText('lab-summary-state', 'Awaiting analysis');
    setText('lab-summary-decision', 'Standby');
  }

  function showAnalysing(strategyName, symbol) {
    var body = document.getElementById('lab-decision-body');
    clear(body);
    var sk = node('div', 'cc-skel'); sk.setAttribute('aria-hidden', 'true');
    sk.appendChild(node('i')); sk.appendChild(node('i')); sk.appendChild(node('i'));
    body.appendChild(sk);
    body.appendChild(node('p', 'note', 'Analysing ' + symbol + ' with ' + strategyName + '…'));
    setText('cc-decision-meta', 'Analysing…');
  }

  function explain(result) {
    var name = result.strategy && result.strategy.name || 'The selected strategy';
    var first = (result.reasons && result.reasons[0]) || '';
    var dataBad = result.context && result.context.state === 'data_unavailable';
    if (dataBad) return 'No decision can be made because the market data is not reliable. ' + first;
    if (result.decision === 'BUY' || result.decision === 'SELL') {
      return 'A qualifying ' + (result.direction === 'bullish' ? 'bullish' : 'bearish') + ' setup has been identified by ' + name +
        '. Review the proposed levels, then let the Risk Engine check the trade before anything can be executed.';
    }
    if (result.decision === 'WATCH') return 'A potential setup is developing, but a required condition is still outstanding. ' + first;
    return first ? first : 'No qualifying setup is currently available.';
  }

  function list(parent, items, className) {
    var ul = node('ul', className || 'lab-evidence-list');
    items.forEach(function (t) { ul.appendChild(node('li', '', t)); });
    parent.appendChild(ul);
  }

  function renderAnalysis(result) {
    if (!result || !result.decision || !result.timeframes || !result.data) throw new Error('The analysis response was incomplete.');
    lastResult = result;
    var decision = document.getElementById('lab-decision-body');
    clear(decision);
    var top = node('div', 'cc-decision-top');
    var badge = node('p', 'lab-decision-badge', result.decision);
    badge.setAttribute('data-decision', result.decision);
    top.appendChild(badge);
    var what = node('div', 'cc-decision-what');
    what.appendChild(node('p', 'cc-decision-strategy', (result.strategy ? result.strategy.name + ' v' + result.strategy.version : 'Strategy') + ' · ' + result.symbol));
    what.appendChild(node('p', 'cc-decision-text', explain(result)));
    top.appendChild(what);
    decision.appendChild(top);

    var dataBad = result.context && result.context.state === 'data_unavailable';
    if (dataBad) {
      var prob = node('p', 'cc-note-problem', 'Data problem: ' + ((result.reasons && result.reasons[0]) || 'the H1 or M5 data is stale, missing or inconsistent.'));
      prob.setAttribute('data-kind', 'error');
      decision.appendChild(prob);
    }
    if (result.stages) {
      var stages = node('ol', 'cc-stages');
      Object.keys(STAGE_TITLES).forEach(function (key) {
        var st = result.stages[key] || { state: 'unknown', detail: '' };
        var li = node('li', 'cc-stage'); li.setAttribute('data-state', st.state); li.title = st.detail || '';
        li.appendChild(node('b', '', STAGE_TITLES[key]));
        li.appendChild(node('span', '', STAGE_WORDS[st.state] || st.state));
        stages.appendChild(li);
      });
      decision.appendChild(stages);
    }
    var ev = node('details', 'lab-intelligence');
    ev.appendChild(node('summary', '', 'Evidence and reasons'));
    if (result.reasons && result.reasons.length) { ev.appendChild(node('p', 'note', 'Why this decision')); list(ev, result.reasons); }
    if (result.confirmation_evidence && result.confirmation_evidence.length) { ev.appendChild(node('p', 'note', 'Evidence found')); list(ev, result.confirmation_evidence); }
    if (result.missing_evidence && result.missing_evidence.length) { ev.appendChild(node('p', 'note', 'Still needed')); list(ev, result.missing_evidence); }
    if (result.gates && result.gates.length) {
      ev.appendChild(node('p', 'note', 'Checks'));
      list(ev, result.gates.map(function (g) { return (g.passed ? '✓ ' : '✕ ') + String(g.id || g.name || 'check').replace(/_/g, ' ') + ': ' + (g.detail || ''); }));
    }
    (result.setup_candidates || []).forEach(function (candidate) {
      var card = node('article', 'lab-candidate');
      card.appendChild(node('h3', 'lab-timeframe-title', candidate.direction + ' · ' + candidate.status));
      card.appendChild(node('p', 'note', candidate.premise));
      (candidate.invalidation_conditions || []).forEach(function (t) { card.appendChild(node('p', 'lab-intelligence-line', 'Invalidated if: ' + t)); });
      card.appendChild(node('p', 'note', 'Target approach: ' + candidate.target_methodology));
      ev.appendChild(card);
    });
    if (result.disclaimer) ev.appendChild(node('p', 'note', result.disclaimer));
    decision.appendChild(ev);
    setText('cc-decision-meta', result.setup_status ? result.setup_status : '');
    setText('lab-summary-state', result.setup_status || result.decision);
    setText('lab-summary-decision', result.decision + (result.direction ? ' · ' + result.direction : ''));

    // ---- H1 context and M5 setup (the only two timeframes either strategy reads) ----
    var mtf = document.getElementById('lab-mtf-body');
    clear(mtf);
    if (result.context) mtf.appendChild(node('p', 'lab-context-summary', result.context.label + ': ' + result.context.summary));
    [['H1', 'market context'], ['M5', 'setup and confirmation']].forEach(function (pair) {
      var tf = pair[0], analysis = result.timeframes[tf], data = result.data[tf];
      var card = node('section', 'lab-timeframe-card');
      card.appendChild(node('h3', 'lab-timeframe-title', tf + ' · ' + pair[1] + ' · ' +
        (analysis && analysis.analyzed ? analysis.bias + ' (' + analysis.strength + ')' : 'not analysed')));
      row(card, 'Data', data ? data.status + (typeof data.lag_seconds === 'number' ? ' · ' + Math.round(data.lag_seconds) + 's old' : '') : 'unavailable');
      if (analysis && analysis.analyzed) {
        if (analysis.events && analysis.events.length) {
          var last = analysis.events[analysis.events.length - 1];
          row(card, 'Latest closed break', last.type + ' ' + last.direction + ' at ' + last.level);
        }
        row(card, 'Structure invalidation', analysis.invalidation && typeof analysis.invalidation.level === 'number' ? String(analysis.invalidation.level) : 'not available');
        card.appendChild(node('p', 'note', analysis.bias_basis || 'No structural explanation supplied.'));
      } else if (data) card.appendChild(node('p', 'note', data.reason || 'Timeframe data unavailable.'));
      mtf.appendChild(card);
    });

    // ---- proposed levels (real values only) and the review area ----
    var setup = document.getElementById('lab-setup-body');
    clear(setup);
    var qualified = (result.decision === 'BUY' || result.decision === 'SELL') && result.entry && result.stop_loss && result.take_profit_1;
    if (qualified) {
      var levels = node('div', 'lab-level-grid');
      row(levels, 'Entry reference', result.entry.price);
      row(levels, 'Stop-loss', result.stop_loss.price);
      row(levels, 'Take-profit', result.take_profit_1.price);
      row(levels, 'Reward-to-risk', result.risk_reward && result.risk_reward.rr_tp1 ? result.risk_reward.rr_tp1.toFixed(2) + 'R' : 'not available');
      row(levels, 'Invalidation', result.setup_invalidation && typeof result.setup_invalidation.level === 'number' ? result.setup_invalidation.level + ' · ' + result.setup_invalidation.timeframe : 'not available');
      setup.appendChild(levels);
      setup.appendChild(node('p', 'note', 'The entry is a reference price (the last closed M5 close), not the fill. The Risk Engine sizes the trade from the live price when you press REVIEW TRADE.'));
    } else {
      setup.appendChild(node('p', 'note', result.decision === 'WATCH' ? 'A setup is developing but is not qualified, so there are no levels to review yet.' :
        result.setup_status === 'invalidated' ? 'The setup is invalidated; do not use its former levels.' : 'No qualified setup, so there is nothing to review. No order was sent.'));
      (result.missing_evidence || []).forEach(function (text) { setup.appendChild(node('p', 'note', 'Still needed: ' + text)); });
    }
    show('cc-review', !!qualified);
    resetReview();
    if (result.clock) {
      var freshness = result.clock.known ? result.clock.detail : 'Broker clock unknown; the strategy will not confirm a setup.';
      setText('lab-status-line', result.symbol + ' · ' + freshness + ' Analysis is read-only; no order was sent.');
    }
  }

  // ---------------------------------------------------------------------------------------------------------------------------
  // REVIEW TRADE (Risk Engine, sends nothing) and CONFIRM & EXECUTE (explicit second step; the server re-validates everything).
  // ---------------------------------------------------------------------------------------------------------------------------
  function fmt(n, d) { return typeof n === 'number' && isFinite(n) ? n.toFixed(d === undefined ? 2 : d) : '—'; }
  function postJson(url, body) {
    return fetch(url, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { j.__status = r.status; return j; }, function () { return { ok: false, message: 'The server sent an unreadable answer (HTTP ' + r.status + ').', __status: r.status }; }); });
  }
  function verdict(kind, title, lines) {
    var box = node('div', 'cc-verdict'); box.setAttribute('data-verdict', kind);
    box.appendChild(node('h3', '', title));
    if (lines && lines.length) list(box, lines, '');
    return box;
  }
  function reviewTrade() {
    if (reviewBusy || !lastResult || !lastSymbol) return;
    reviewBusy = true;
    var btn = document.getElementById('cc-review-btn'); btn.disabled = true;
    var out = document.getElementById('cc-review-result'); clear(out);
    out.appendChild(node('p', 'note', 'Asking the Risk Engine…'));
    var sent = lastEpoch;
    postJson('/api/pipeline/propose', { symbol: lastSymbol }).then(function (body) {
      if (sent !== lastEpoch) return;                           // another analysis or a strategy change replaced this one
      clear(out);
      reviewBusy = false;
      if (!body.ok) { out.appendChild(verdict('rejected', 'The review could not run', [body.message || 'Unknown problem. Nothing was sent.'])); btn.disabled = false; return; }
      if (body.approved && body.approval) {
        approval = body.approval;
        var sm = approval.summary;
        var box = verdict('approved', 'Approved by the Risk Engine. Nothing has been sent.', []);
        var grid = node('div', 'lab-level-grid');
        row(grid, 'Strategy', sm.strategy_id ? (lastResult.strategy ? lastResult.strategy.name : sm.strategy_id) + ' v' + sm.strategy_version : '—');
        row(grid, 'Instrument', sm.symbol);
        row(grid, 'Direction', sm.direction.toUpperCase());
        row(grid, 'Entry (live price)', sm.entry_live);
        row(grid, 'Stop-loss', sm.stop_loss);
        row(grid, 'Take-profit', sm.take_profit);
        row(grid, 'Reward-to-risk (live)', fmt(sm.reward_to_risk_live) + 'R');
        row(grid, 'Position size', sm.volume + ' lots');
        row(grid, 'Estimated risk', fmt(sm.risk_amount) + ' ' + (sm.currency || '') + ' (' + fmt(sm.risk_percent, 2) + '%)');
        box.appendChild(grid);
        (body.warnings || []).forEach(function (w) { box.appendChild(node('p', 'note', w)); });
        out.appendChild(box);
        show('cc-execute', true);
        var ttl = Math.max(1, Math.round(approval.expires_at - approval.created_at));
        var deadline = Date.now() + ttl * 1000;
        setText('cc-exec-state', 'Approved. Waiting for your confirmation.');
        if (countdownTimer) clearInterval(countdownTimer);
        var tick = function () {
          var left = Math.round((deadline - Date.now()) / 1000);
          var ex = document.getElementById('cc-execute-btn');
          if (left <= 0) {
            clearInterval(countdownTimer); countdownTimer = null; approval = null;
            if (ex) { ex.disabled = true; ex.removeAttribute('data-armed'); ex.textContent = 'CONFIRM & EXECUTE'; }
            show('cc-cancel-btn', false);
            setText('cc-execute-note', 'The approval expired. Press REVIEW TRADE again; market data and account state must be checked again.');
            setText('cc-exec-state', 'Approval expired.');
            btn.disabled = false;
          } else setText('cc-execute-note', 'Valid for ' + left + ' s. It is single-use and is cancelled if the account, the strategy or the setup changes.');
        };
        document.getElementById('cc-execute-btn').disabled = false;
        tick(); countdownTimer = setInterval(tick, 1000);
      } else {
        var reasons = (body.reasons && body.reasons.length ? body.reasons : [body.message]).slice(0, 6);
        var title = body.stages && body.stages.risk_approved === false ? 'Rejected by the Risk Engine' :
          body.stages && body.stages.strategy_qualified === false ? 'The setup no longer qualifies' : 'Not eligible to trade';
        out.appendChild(verdict('rejected', title + '. Nothing was sent.', reasons));
        setText('cc-exec-state', 'Rejected: ' + (body.code || 'see reasons'));
        btn.disabled = false;
      }
    }).catch(function () {
      if (sent !== lastEpoch) return;
      clear(out); reviewBusy = false; btn.disabled = false;
      out.appendChild(verdict('rejected', 'The review failed. Nothing was sent.', ['Could not reach TradeMate. Check that it is running.']));
    });
  }
  function disarm() {
    var ex = document.getElementById('cc-execute-btn');
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
    if (ex) { ex.removeAttribute('data-armed'); ex.textContent = 'CONFIRM & EXECUTE'; }
    show('cc-cancel-btn', false);
  }
  function executeTrade() {
    var ex = document.getElementById('cc-execute-btn');
    if (executeBusy || !approval) return;
    if (ex.getAttribute('data-armed') !== 'true') {              // first click arms; the second click sends
      var sm = approval.summary;
      ex.setAttribute('data-armed', 'true');
      ex.textContent = 'SEND NOW: ' + sm.direction.toUpperCase() + ' ' + sm.volume + ' ' + sm.symbol;
      show('cc-cancel-btn', true);
      armTimer = setTimeout(disarm, 6000);
      return;
    }
    executeBusy = true; ex.disabled = true; disarm();
    var id = approval.id;
    setText('cc-execute-note', 'Re-validating and sending…');
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    postJson('/api/pipeline/execute', { approval_id: id, confirm: true }).then(function (body) {
      executeBusy = false; approval = null;
      var out = document.getElementById('cc-review-result'); clear(out);
      var ok = body.outcome === 'executed';
      var lines = [body.message || 'No message.'];
      if (body.refusal) lines.push('Reason: ' + body.refusal.message);
      if (body.placement_check && body.placement_check.matches === false) lines = lines.concat(body.placement_check.discrepancies);
      if (body.warning) lines.push(body.warning);
      if (body.next_action) lines.push(body.next_action);
      out.appendChild(verdict(ok ? 'approved' : 'rejected', ok ? 'Executed and confirmed in the account.' : 'Not executed (' + (body.outcome || 'unknown') + ').', lines));
      show('cc-execute', false);
      setText('cc-exec-state', ok ? 'Position confirmed by MT5.' : 'Order not confirmed: ' + (body.outcome || 'unknown'));
      document.getElementById('cc-review-btn').disabled = false;
      loadActivity();
    }).catch(function () {
      executeBusy = false; approval = null; show('cc-execute', false);
      setText('cc-exec-state', 'Outcome unknown.');
      var out = document.getElementById('cc-review-result'); clear(out);
      out.appendChild(verdict('rejected', 'The answer was lost. The order may or may not exist.', ['Check MT5 and the execution journal before doing anything else. Do not retry blindly.']));
    });
  }
  var reviewBtn = document.getElementById('cc-review-btn'), executeBtn = document.getElementById('cc-execute-btn'), cancelBtn = document.getElementById('cc-cancel-btn');
  if (reviewBtn) reviewBtn.addEventListener('click', reviewTrade);
  if (executeBtn) executeBtn.addEventListener('click', executeTrade);
  if (cancelBtn) cancelBtn.addEventListener('click', disarm);

  function searchMarkets() {
    var input = document.getElementById('lab-market'), list = document.getElementById('lab-market-options');
    var query = input.value.trim(), mine = ++searchSeq;
    if (searchController) searchController.abort();
    clear(list);
    if (query.length < 1) return;
    searchController = ('AbortController' in window) ? new AbortController() : null;
    fetch('/api/symbols/search?limit=30&q=' + encodeURIComponent(query), {
      cache: 'no-store', signal: searchController ? searchController.signal : undefined
    }).then(function (response) {
      if (!response.ok) throw new Error('Broker symbol search failed (HTTP ' + response.status + ').');
      return response.json();
    }).then(function (body) {
      if (mine !== searchSeq) return;
      if (!body.ok) throw new Error(body.message || 'Broker symbol search failed.');
      (body.matches || []).forEach(function (match) {
        if (!match || typeof match.name !== 'string') return;
        var option = document.createElement('option');
        option.value = match.name;
        option.label = [match.description, match.path].filter(Boolean).join(' · ');
        list.appendChild(option);
      });
      document.getElementById('lab-status-line').textContent = body.matches && body.matches.length ?
        'Select a broker-supported instrument from the suggestions.' : 'No matching instruments were returned by the broker.';
    }).catch(function (error) {
      if (mine !== searchSeq || error.name === 'AbortError') return;
      document.getElementById('lab-status-line').textContent = error.message || 'Could not search broker instruments.';
    });
  }
  function closeSavedMarkets() {
    var dropdown = document.getElementById('lab-market-dropdown');
    var menu = document.getElementById('lab-market-saved');
    if (!dropdown || !menu) return;
    menu.hidden = true;
    dropdown.setAttribute('aria-expanded', 'false');
  }
  function showSavedMarketsMessage(message, isError) {
    var menu = document.getElementById('lab-market-saved');
    if (!menu) return;
    clear(menu);
    var note = node('p', 'lab-market-saved-note', message);
    if (isError) note.setAttribute('data-error', 'true');
    menu.appendChild(note);
  }
  function selectSavedMarket(symbol) {
    var input = document.getElementById('lab-market');
    if (!input) return;
    input.value = symbol;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    closeSavedMarkets();
    input.focus();
  }
  function loadSavedMarkets() {
    var dropdown = document.getElementById('lab-market-dropdown');
    var menu = document.getElementById('lab-market-saved');
    if (!dropdown || !menu || dropdown.disabled) return;
    if (!menu.hidden) {
      closeSavedMarkets();
      return;
    }
    menu.hidden = false;
    dropdown.setAttribute('aria-expanded', 'true');
    showSavedMarketsMessage('Loading instruments from MT5 Market Watch…');
    fetch('/api/broker', { cache: 'no-store' }).then(function (response) {
      if (!response.ok) throw new Error('MT5 instruments could not be loaded (HTTP ' + response.status + ').');
      return response.json();
    }).then(function (body) {
      if (!body || !body.status || body.status.state !== 'CONNECTED') {
        throw new Error((body && body.status && body.status.message) || 'MT5 is not connected.');
      }
      var symbols = Array.isArray(body.symbols) ? body.symbols.filter(function (symbol) {
        return symbol && typeof symbol.name === 'string' && symbol.name.trim();
      }) : [];
      clear(menu);
      if (!symbols.length) {
        showSavedMarketsMessage('No instruments are currently saved in MT5 Market Watch.');
        return;
      }
      symbols.forEach(function (symbol) {
        var option = node('button', 'lab-market-saved-option');
        option.type = 'button';
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.setAttribute('data-symbol', symbol.name);
        option.appendChild(node('strong', '', symbol.name));
        if (symbol.description && symbol.description !== symbol.name) {
          option.appendChild(node('small', '', symbol.description));
        }
        menu.appendChild(option);
      });
    }).catch(function (error) {
      showSavedMarketsMessage(error.message || 'Could not load MT5 Market Watch.', true);
    });
  }
  function currentStrategyName() {
    if (!catalogue || !catalogue.selected) return 'the selected strategy';
    return catalogue.selected.name;
  }
  function analyse() {
    var input = document.getElementById('lab-market'), symbol = input.value.trim();
    if (analysing) return;
    if (!catalogue || !catalogue.selected_id) {
      setText('lab-status-line', 'No valid strategy is selected. Choose CHoCH Trading or Trend Continuation first.');
      var sel = document.getElementById('cc-strategy'); if (sel) sel.focus();
      return;
    }
    if (!symbol) {
      setText('lab-status-line', 'Search for and select an instrument first.');
      input.focus();
      return;
    }
    setAnalysisBusy(true);
    clearAnalysis();
    showAnalysing(currentStrategyName(), symbol);
    setText('lab-status-line', 'Validating ' + symbol + ' with the connected broker…');
    if (analysisController) analysisController.abort();
    analysisController = ('AbortController' in window) ? new AbortController() : null;
    var signal = analysisController ? analysisController.signal : undefined;
    var epochAtStart = catalogue.epoch;
    fetch('/api/symbols/add', {
      method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: symbol })
    }).then(function (response) {
      if (!response.ok) throw new Error('The broker could not validate that instrument (HTTP ' + response.status + ').');
      return response.json();
    }).then(function (added) {
      if (!added.ok || !added.symbol || !added.symbol.name) throw new Error(added.message || 'The broker did not validate that instrument.');
      input.value = added.symbol.name;
      lastSymbol = added.symbol.name;
      setText('lab-summary-market', added.symbol.name);
      setText('lab-status-line', 'Analysing ' + added.symbol.name + ' with ' + currentStrategyName() + '…');
      return fetch('/api/strategy/analyse?symbol=' + encodeURIComponent(added.symbol.name), { cache: 'no-store', signal: signal });
    }).then(function (response) {
      return response.json().then(function (body) { body.__status = response.status; return body; });
    }).then(function (body) {
      if (body.stale) throw new Error(body.message);
      if (!body.ok || !body.result) throw new Error(body.message || 'The strategy returned no analysis.');
      if (catalogue.epoch !== epochAtStart) throw new Error('The strategy changed while this analysis ran, so its result was discarded. Run it again.');
      lastEpoch = {};                                           // a new identity for this analysis: older reviews can no longer apply to it
      renderAnalysis(body.result);
    }).catch(function (error) {
      if (error.name === 'AbortError') return;
      clearAnalysis(error.message ? 'No result: ' + error.message : 'Analysis is unavailable. No result or levels were invented.');
      setText('lab-status-line', error.message || 'Analysis failed. Check the broker connection and try again.');
    }).then(function () {
      setAnalysisBusy(false);
    });
  }
  function render(body) {
    var byId = {};
    (body.capabilities || []).forEach(function (c) { byId[c.id] = c; });
    Array.prototype.forEach.call(states(), function (n) {
      var c = byId[n.getAttribute('data-cap')];
      if (!c) { n.setAttribute('data-state', 'Unavailable'); n.textContent = 'Unavailable'; return; }
      n.setAttribute('data-state', c.state); n.textContent = c.state; n.title = c.detail;
    });
    renderWatchdog(body.watchdog);
    handleWatchdogEvents(body.watchdog);
    activityMonitor = body.watchdog;
    var broker = byId.broker;
    document.getElementById('lab-broker-note').textContent = broker ? broker.detail : 'Connection state unknown.';
    setText('lab-build', 'Part ' + body.part + ' of the roadmap');
    // A control is enabled only when its backend capability is Available.
    [['lab-analyse', 'analyse'], ['lab-market', 'selector'], ['lab-watchdog', 'watchdog'], ['lab-autopilot', 'autopilot']].forEach(function (p) {
      var el = document.getElementById(p[0]), c = byId[p[1]];
      if (el) {
        var available = !!(c && c.state === 'Available');
        if (p[0] === 'lab-analyse') {
          // Allow the user to attempt analysis while disconnected/stale so the API can
          // explain the real blocker. This does not bypass server-side broker/data checks.
          el.setAttribute('data-ready', 'true');
          el.disabled = analysing;
        } else if (p[0] === 'lab-market') {
          el.setAttribute('data-ready', 'true');
          el.disabled = false;
        } else if (p[0] === 'lab-watchdog') {
          // Monitoring is a toggle, so it must remain enabled while active to permit stop.
          // When stopped, broker capability determines whether starting can be attempted.
          var brokerAvailable = !!(byId.broker && byId.broker.state === 'Available');
          el.setAttribute('data-ready', String(brokerAvailable || !!(body.watchdog && body.watchdog.active)));
          el.disabled = !brokerAvailable && !(body.watchdog && body.watchdog.active);
        } else {
          el.setAttribute('data-ready', String(available));
          el.disabled = !available;
        }
      }
    });
    var marketDropdown = document.getElementById('lab-market-dropdown');
    if (marketDropdown) {
      var selector = byId.selector;
      var available = !!(selector && selector.state === 'Available');
      marketDropdown.setAttribute('data-ready', 'true');
      marketDropdown.disabled = analysing;
      // Keep the saved-market control clickable; its request reports broker availability.
    }
    // Status polling is independent of an explicit analysis request. Do not let a
    // background poll erase the result/error produced by that request or overwrite
    // its progress message while it is in flight. A disconnected broker is reported
    // in the broker note; the analysis endpoint remains the source of the detailed
    // data-quality reason. This does not make stale data eligible for trading.
    if (!analysing) {
      if (!(broker && broker.state === 'Available')) {
        document.getElementById('lab-status-line').textContent = 'MT5 is not connected. You can still request analysis to see the specific blocker; no trade can be confirmed without valid data.';
      } else {
        document.getElementById('lab-status-line').textContent = 'Status updated. MT5 is connected.';
      }
    }
  }
  function load() {
    var mine = ++seq;
    fetch('/api/strategylab/status')
      .then(function (r) { return r.json(); })
      .then(function (body) { if (mine === seq && body && body.ok) { render(body); loadCatalogue(); loadHeader(false); loadAutopilot(); loadActivity(); } else if (mine === seq) throw new Error('bad'); })
      .catch(function () {
        if (mine !== seq) return;
        setAll('Unavailable', 'Unavailable');
        setAnalysisBusy(false);
        var watchdogCheckbox = document.getElementById('lab-watchdog');
        // If the status endpoint itself fails, don't strand the control in a disabled
        // state. Let the start/stop endpoint validate the request and return its own
        // actionable error; it is the authority for whether monitoring can start.
        if (watchdogCheckbox) watchdogCheckbox.disabled = false;
        ['lab-market', 'lab-analyse', 'lab-market-dropdown'].forEach(function (id) {
          var control = document.getElementById(id);
          if (control) { control.setAttribute('data-ready', 'true'); control.disabled = false; }
        });
        closeSavedMarkets();
        document.getElementById('lab-broker-note').textContent = 'TradeMate could not report its status.';
        document.getElementById('lab-status-line').textContent = 'Could not load Command Centre status. Nothing here is operational.';
      });
  }
  // ---------------------------------------------------------------------------------------------------------------------------
  // Watchdog label, strategy selector, header status, Autopilot readiness and recent activity. All values come from the backend.
  // ---------------------------------------------------------------------------------------------------------------------------
  function renderWatchdogLabel(monitor) {
    var el = document.getElementById('cc-wd-state'); if (!el) return;
    var kind = 'off', text = 'Off';
    if (monitor && monitor.active) {
      if (monitor.state === 'Monitoring' || monitor.state === 'Waiting for Setup') { kind = 'active'; text = 'Active' + (monitor.state === 'Waiting for Setup' ? ' · waiting for a setup' : ''); }
      else if (monitor.state === 'Stopping') { kind = 'starting'; text = 'Stopping'; }
      else { kind = 'unavailable'; text = 'Temporarily unavailable · ' + monitor.state; }
    } else if (monitor && monitor.state === 'Strategy Changed') { kind = 'off'; text = 'Off · the strategy changed'; }
    else if (monitor && /unavailable|disconnected|stale|incomplete/i.test(monitor.state || '')) { kind = 'unavailable'; text = 'Temporarily unavailable · ' + monitor.state; }
    el.setAttribute('data-wd', kind); el.textContent = text + (monitor && monitor.active && monitor.strategy_id ? ' · ' + strategyNameFor(monitor.strategy_id) : '');
  }
  function strategyNameFor(id) {
    var found = catalogue && catalogue.strategies ? catalogue.strategies.filter(function (x) { return x.id === id; })[0] : null;
    return found ? found.name : id;
  }
  function setChip(id, tone, text, hidden) {
    var chip = document.getElementById(id); if (!chip) return;
    chip.setAttribute('data-tone', tone); chip.hidden = !!hidden;
    var t = document.getElementById(id + '-text'); if (t) t.textContent = text;
  }
  function renderCatalogue(body) {
    var previousEpoch = catalogue ? catalogue.epoch : null, previousId = catalogue ? catalogue.selected_id : null;
    catalogue = body;
    var select = document.getElementById('cc-strategy');
    if (select) {
      clear(select);
      if (!body.strategies || !body.strategies.length) { select.appendChild(node('option', '', 'No strategies available')); select.disabled = true; }
      else {
        if (!body.selected_id) { var none = node('option', '', 'Choose a strategy…'); none.value = ''; select.appendChild(none); }
        body.strategies.forEach(function (st) { var o = node('option', '', st.name); o.value = st.id; select.appendChild(o); });
        select.value = body.selected_id || '';
        select.disabled = false;
      }
    }
    var sel = body.selected;
    setText('cc-strategy-desc', sel ? sel.summary + ' ' + sel.description : (body.problem || 'No valid strategy is selected. Choose one to analyse.'));
    show('cc-strategy-tf', !!sel);
    setChip('cc-chip-strategy', sel ? 'ok' : 'bad', sel ? 'Strategy: ' + sel.name + ' · v' + sel.version : 'No strategy selected', false);
    var analyseButton = document.getElementById('lab-analyse');
    if (analyseButton) analyseButton.title = sel ? 'Analyse with ' + sel.name : 'Select a strategy first';
    // the selection changed elsewhere (another tab): whatever is on screen belongs to the old strategy
    if (previousId !== null && (previousId !== body.selected_id || previousEpoch !== body.epoch) && lastResult) {
      clearAnalysis('The strategy changed, so the previous result was cleared. Run ANALYSE MARKET again.');
    }
  }
  function loadCatalogue() {
    return fetch('/api/strategy/catalogue', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (body) {
      if (body && body.ok) renderCatalogue(body);
    }).catch(function () {
      setChip('cc-chip-strategy', 'bad', 'Strategy: unavailable', false);
      setText('cc-strategy-desc', 'The strategy catalogue could not be loaded. Nothing can be analysed until it loads.');
    });
  }
  function changeStrategy(event) {
    var select = event.target, wanted = select.value, before = catalogue ? catalogue.selected_id : null;
    if (!wanted || wanted === before) return;
    select.disabled = true;
    setText('lab-status-line', 'Switching strategy…');
    postJson('/api/strategy/select', { strategy: wanted }).then(function (body) {
      if (!body.ok) throw new Error(body.message || 'The strategy could not be changed.');
      renderCatalogue(body);
      clearAnalysis('Strategy changed to ' + body.selected.name + '. Any pending review was cancelled. Run ANALYSE MARKET to analyse with it.');
      setText('lab-status-line', 'Strategy changed to ' + body.selected.name + '. Pending proposals from the previous strategy were cancelled and the Watchdog was reset.');
      load();
    }).catch(function (error) {
      select.value = before || '';
      setText('lab-status-line', error.message || 'The strategy could not be changed.');
    }).then(function () { select.disabled = false; });
  }
  var strategySelect = document.getElementById('cc-strategy');
  if (strategySelect) strategySelect.addEventListener('change', changeStrategy);

  var lastBrokerAt = 0;
  function loadHeader(force) {
    var now = Date.now();
    if (!force && now - lastBrokerAt < 30000) return;
    lastBrokerAt = now;
    fetch('/api/broker', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (b) {
      var st = b.status || {}, connected = st.state === 'CONNECTED';
      setChip('cc-chip-conn', connected ? 'ok' : (st.state === 'STALE' ? 'warn' : 'bad'), connected ? 'MT5 connected' : 'MT5 ' + String(st.state || 'unavailable').toLowerCase(), false);
      var a = b.account, kv = document.getElementById('cc-account-kv');
      if (connected && a) {
        var env = String(a.environment || '').toUpperCase();
        var chip = document.getElementById('cc-chip-account');
        if (chip) chip.setAttribute('data-env', env === 'REAL' ? 'live' : 'demo');
        setChip('cc-chip-account', env === 'DEMO' ? 'ok' : 'warn', (env === 'REAL' ? 'LIVE' : env || 'ACCOUNT') + ' · ' + a.login, false);
        if (kv) {
          clear(kv); kv.hidden = false;
          [['Mode', env === 'REAL' ? 'LIVE' : env], ['Login', a.login], ['Server', a.server], ['Balance', a.balance === null ? '—' : fmt(a.balance) + ' ' + a.currency],
           ['Equity', a.equity === null ? '—' : fmt(a.equity) + ' ' + a.currency]].forEach(function (p) {
            kv.appendChild(node('dt', '', p[0])); kv.appendChild(node('dd', '', p[1]));
          });
        }
      } else { setChip('cc-chip-account', 'idle', '', true); if (kv) { clear(kv); kv.hidden = true; } }
    }).catch(function () { setChip('cc-chip-conn', 'bad', 'TradeMate status unavailable', false); });
  }
  function loadAutopilot() { /* Autopilot is controlled and displayed by commandcentre.js (the Trading controls). */ }

  function when(value) {
    var t = typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value);
    return isFinite(t) ? new Date(t) : null;
  }
  var activityMonitor = null;
  function loadActivity() {
    fetch('/api/executions', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (b) {
      var items = [];
      ((activityMonitor && activityMonitor.events) || []).forEach(function (e) {
        var d = when(e.timestamp);
        var text = e.event_type === 'choch' ? 'CHoCH alert: ' + e.direction + ' ' + e.symbol + ' ' + e.timeframe : 'Setup alert: ' + e.decision + ' ' + e.symbol + (e.strategy_id ? ' · ' + strategyNameFor(e.strategy_id) : '');
        items.push({ d: d, kind: 'alert', text: text });
      });
      ((b && b.executions) || []).slice(0, 8).forEach(function (x) {
        var d = when(x.created_at);
        items.push({ d: d, kind: (x.state === 'confirmed' || x.state === 'reconciled_executed') ? 'trade' : (x.state === 'unknown' || x.state === 'unconfirmed' || x.state === 'rejected') ? 'problem' : 'note', text: (x.action || 'order') + ' ' + (x.side || '') + ' ' + (x.volume || '') + ' ' + (x.symbol || '') + ' · ' + (x.state || '') + (x.message ? ' · ' + x.message : '') });
      });
      items.sort(function (p, q) { return (q.d ? q.d.getTime() : 0) - (p.d ? p.d.getTime() : 0); });
      var ul = document.getElementById('cc-activity'); if (!ul) return;
      clear(ul);
      if (!items.length) { ul.appendChild(node('li', 'note', 'No recorded activity yet.')); return; }
      items.slice(0, 8).forEach(function (it) {
        var li = node('li'); li.setAttribute('data-kind', it.kind);
        li.appendChild(node('time', '', it.d ? it.d.toLocaleString() : '—')); li.appendChild(node('span', '', it.text));
        ul.appendChild(li);
      });
    }).catch(function () {
      var ul = document.getElementById('cc-activity'); if (ul) { clear(ul); ul.appendChild(node('li', 'note', 'Recent activity could not be loaded.')); }
    });
  }

  function visible() { return !view.hidden; }
  function tick() { if (visible()) { load(); if (greeter) greeter.render(); } }
  var marketInput = document.getElementById('lab-market');
  var savedMarketsButton = document.getElementById('lab-market-dropdown');
  var savedMarketsMenu = document.getElementById('lab-market-saved');
  if (savedMarketsButton) savedMarketsButton.addEventListener('click', loadSavedMarkets);
  if (savedMarketsMenu) savedMarketsMenu.addEventListener('click', function (event) {
    var option = event.target.closest('[data-symbol]');
    if (option && savedMarketsMenu.contains(option)) selectSavedMarket(option.getAttribute('data-symbol'));
  });
  document.addEventListener('click', function (event) {
    if (savedMarketsMenu && !event.target.closest('.lab-market-picker')) closeSavedMarkets();
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && savedMarketsMenu && !savedMarketsMenu.hidden) {
      closeSavedMarkets();
      if (savedMarketsButton) savedMarketsButton.focus();
    }
  });
  if (marketInput) {
    marketInput.addEventListener('input', function () {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(searchMarkets, 220);
    });
    marketInput.addEventListener('change', function () {
      document.getElementById('lab-summary-market').textContent = marketInput.value.trim() || 'Not selected';
      clearAnalysis('Run ANALYSE to load current broker data for the selected instrument.');
      if (watchdogState && watchdogState.active && watchdogState.symbol !== marketInput.value.trim()) {
        document.getElementById('lab-watchdog-note').textContent = 'Stopping monitoring because the selected instrument changed.';
        document.getElementById('lab-watchdog').checked = false;
        requestWatchdog(false);
      }
    });
    marketInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        analyse();
      }
    });
  }
  var analyseButton = document.getElementById('lab-analyse');
  if (analyseButton) analyseButton.addEventListener('click', analyse);
  var watchdogCheckbox = document.getElementById('lab-watchdog');
  if (watchdogCheckbox) watchdogCheckbox.addEventListener('change', function () { requestWatchdog(watchdogCheckbox.checked); });
  window.TradeMateRouter.on(function () { if (visible()) { load(); if (greeter) greeter.render(); } });
  timer = setInterval(tick, REFRESH_MS);
  if (visible()) load();
  // The Command Centre sync (commandcentre.js) renders the backend's authoritative decision through these two functions.
  window.TradeMateStrategyLab = { reload: load,
    show: function (result) { lastEpoch = {}; renderAnalysis(result); },
    clear: function (message) { clearAnalysis(message); },
    current: function () { return lastResult; } };
})();
