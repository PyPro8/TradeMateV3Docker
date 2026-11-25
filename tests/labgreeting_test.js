// Run with: node tests/labgreeting_test.js   (also run by tests/test_labgreeting_js.py)
// Checks static/js/labgreeting.js: the time-of-day boundaries, the welcome-back rule, storage that is missing / blocked / corrupt, and the mounter.
// Dates are built from LOCAL components, so the results do not depend on the computer's timezone or on today's date.
const assert = require('assert');
const G = require('../static/js/labgreeting.js');
let checks = 0;
const eq = (a, b, m) => { checks++; assert.strictEqual(a, b, `${m || ''} -> got ${JSON.stringify(a)}, expected ${JSON.stringify(b)}`); };
const at = (h, mi) => new Date(2026, 9, 21, h, mi || 0, 0);

// part of day, every boundary
[[0, 'evening'], [4, 'evening'], [5, 'morning'], [11, 'morning'], [12, 'afternoon'], [17, 'afternoon'], [18, 'evening'], [23, 'evening']]
  .forEach(([h, p]) => eq(G.partOfDay(h), p, 'hour ' + h));
eq(G.partOfDay(4.99), 'evening', '04:59 is still evening'); eq(G.partOfDay(5), 'morning'); eq(G.partOfDay(11.99), 'morning'); eq(G.partOfDay(17.99), 'afternoon');
[-1, 24, NaN, Infinity, '9', null, undefined].forEach((v) => eq(G.partOfDay(v), null, 'invalid hour ' + String(v)));

// greeting text from a Date
eq(G.greeting(at(4, 59)), 'Good evening'); eq(G.greeting(at(5, 0)), 'Good morning'); eq(G.greeting(at(11, 59)), 'Good morning');
eq(G.greeting(at(12, 0)), 'Good afternoon'); eq(G.greeting(at(17, 59)), 'Good afternoon'); eq(G.greeting(at(18, 0)), 'Good evening'); eq(G.greeting(at(0, 0)), 'Good evening');
['x', null, undefined, new Date('nope'), 5].forEach((v) => eq(G.greeting(v), 'Welcome', 'invalid date ' + String(v)));

// welcome-back rule
const now = at(9, 30), earlier = at(8, 0).getTime();
eq(G.welcome({ now, lastVisit: null }).text, 'Good morning. Welcome to Command Centre.', 'first visit');
eq(G.welcome({ now, lastVisit: earlier }).text, 'Good morning. Welcome back to Command Centre.', 'returning');
eq(G.welcome({ now, lastVisit: earlier }).returning, true);
[now.getTime(), now.getTime() + 1000, 0, -5, NaN, '123', undefined, Infinity].forEach((v) => eq(G.welcome({ now, lastVisit: v }).returning, false, 'not returning for ' + String(v)));
eq(G.welcome({}).text, 'Welcome. Welcome to Command Centre.', 'no clock at all still yields text');
eq(G.welcome().returning, false);

// storage helpers never throw
const mem = () => { const m = {}; return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; }, _m: m }; };
const s = mem();
eq(G.readLastVisit(s), null, 'empty storage'); eq(G.recordVisit(s, 12345), true); eq(G.readLastVisit(s), 12345, 'round trip');
const bad = (raw) => { const m = mem(); m._m[G.KEY] = raw; return G.readLastVisit(m); };
['not json', '{"a":1}', '-5', '0', 'null', '""', 'NaN'].forEach((r) => eq(bad(r), null, 'corrupt value ' + r));
const throwing = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
eq(G.readLastVisit(throwing), null, 'blocked read'); eq(G.recordVisit(throwing, 1), false, 'blocked write'); eq(G.readLastVisit(null), null); eq(G.recordVisit(null, 1), false);
eq(G.recordVisit(s, NaN), false, 'refuses to store NaN');

const names = mem();
eq(G.sanitizeDisplayName('  Alex   Smith  '), 'Alex Smith', 'normalizes spacing');
eq(G.sanitizeDisplayName('<Alex>'), 'Alex', 'removes markup characters');
eq(G.saveDisplayName(names, 'Alex Smith'), 'Alex Smith', 'saves normalized name');
eq(G.readDisplayName(names), 'Alex Smith', 'reads saved preferred name');
eq(G.welcome({ now, lastVisit: earlier, displayName: G.readDisplayName(names) }).text,
  'Good morning, Alex Smith. Welcome back to Command Centre.', 'uses preferred name');
eq(G.saveDisplayName(names, ''), null, 'clears preferred name');
eq(G.readDisplayName(names), '', 'clear persists');
eq(G.readDisplayName(throwing), '', 'blocked preference read is safe');

// mounter: previous visit read once, this visit recorded at once, text follows the injected clock
const el = { textContent: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
const st = mem(); st.setItem(G.KEY, JSON.stringify(at(7, 0).getTime()));
let clock = at(9, 0);
const m = G.mount(el, { storage: st, now: () => clock });
eq(el.textContent, 'Good morning. Welcome back to Command Centre.', 'mounted returning'); eq(el.attrs['data-returning'], 'true'); eq(el.attrs['data-part'], 'Good morning');
eq(G.readLastVisit(st), at(9, 0).getTime(), 'visit recorded immediately'); eq(m.previous(), at(7, 0).getTime());
clock = at(13, 0); m.render();
eq(el.textContent, 'Good afternoon. Welcome back to Command Centre.', 'follows the clock'); eq(m.previous(), at(7, 0).getTime(), 'previous visit stays stable for the session');
const el2 = { textContent: '', setAttribute() {} };
G.mount(el2, { storage: mem(), now: () => at(20, 0) });
eq(el2.textContent, 'Good evening. Welcome to Command Centre.', 'first ever visit');
G.mount(el2, { storage: null, now: () => at(6, 0) });
eq(el2.textContent, 'Good morning. Welcome to Command Centre.', 'no storage available');

console.log(`labgreeting.js: all ${checks} checks passed`);
