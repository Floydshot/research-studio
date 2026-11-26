const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise the actual browser recommendation code against a fixed market-data
// snapshot. No network access, browser state or saved private portfolios.
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
function declaration(name) {
  const marker = '    function ' + name + '(';
  const start = html.indexOf(marker);
  assert.ok(start >= 0, 'application declaration exists: ' + name);
  const rest = html.slice(start + marker.length);
  const boundary = /\n    (?:(?:async )?function |(?:const|let) [A-Za-z_$])/.exec(rest);
  assert.ok(boundary, 'application declaration boundary exists: ' + name);
  return html.slice(start, start + marker.length + boundary.index);
}
function constant(name) {
  const match = html.match(new RegExp('^    const ' + name + ' = [^\\n]+', 'm'));
  assert.ok(match, 'application constant exists: ' + name);
  return match[0];
}

const now = Date.parse('2026-11-26T10:00:00Z');
const start = '2026-11-26T06:31:00Z';
const end = '2026-11-26T06:32:00Z';
const retrieved = '2026-11-26T06:31:30Z';
const quote = '2026-11-25T21:00:00Z';
const batch = () => ({ version: 1, id: start, started_at: start, completed_at: end, status: 'live' });
function marketData() {
  return { schema_version: 1, data_mode: 'live', updated_at: end, refresh_batch: batch() };
}
function stock(overrides = {}) {
  const fields = ['current_pe', 'forward_pe', 'price', 'currency', 'sma200', 'rsi14'];
  return {
    ticker: 'MSFT', name: 'Microsoft', current_pe: 22, forward_pe: 20,
    price: 80, currency: 'USD', sma200: 90, rsi14: 40, as_of: quote,
    technicals_as_of: quote, tranche_key: 'buy',
    field_sources: Object.fromEntries(fields.map(key => [key, 'live'])),
    field_updated_at: Object.fromEntries(fields.map(key => [key, retrieved])),
    refresh: { version: 1, batch_id: start, status: 'live', missing_fields: [] },
    ...overrides
  };
}
function screenedStock({ pass = true, ticker = 'GS' } = {}) {
  const tsm = ticker === 'TSM';
  const candidate = stock({ ticker, hist_pe: 20, current_pe: pass ? 16 : tsm ? 26 : 19, forward_pe: 18, tranche_key: pass ? 'watch' : 'buy' });
  candidate.buy_indicator = {
    version: 1, status: pass ? 'eligible' : 'watch', currency: 'USD',
    as_of: quote, evaluated_at: retrieved,
    conditions: [
      { key: 'current_pe', actual: candidate.current_pe, threshold: tsm ? 25 : 17, passed: pass },
      { key: 'forward_pe', actual: 18, threshold: 20, passed: true },
      { key: 'sma200', actual: 80, threshold: 90, passed: true },
      { key: 'rsi14', actual: 40, threshold: tsm ? 45 : 50, passed: true }
    ]
  };
  return candidate;
}

class FixtureDate extends Date {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
}
const context = vm.createContext({ Date: FixtureDate, console });
vm.runInContext(`
const state={data:null,refreshFailed:false,portfolio:'tech'};
let fixturePolicy=null;
// The surrounding portfolio UI is outside this helper's boundary. Supply its
// public type/policy contract while retaining the application's real decision,
// field-source, freshness, mode and tranche functions below.
function portfolio(){return {tranche_policy:fixturePolicy};}
function futureUsesDividends(){return state.portfolio==='value';}
${['number', 'positive', 'cnnTime'].map(constant).join('\n')}
${['individualOnlyPortfolio', 'isETF', 'source', 'mode', 'dailyRefreshDue', 'batchCurrent', 'shoppingStockFresh', 'trancheKey', 'shoppingRecommendation'].map(declaration).join('\n')}
`, context);
let checks = 0;
function eq(actual, expected, message) { assert.deepEqual(actual, expected, message); checks++; }
function result(candidate, setup = {}) {
  context.candidate = structuredClone(candidate);
  context.fixtureData = structuredClone(setup.data || marketData());
  context.fixturePortfolio = setup.portfolio || 'tech';
  context.fixturePolicyInput = setup.policy || null;
  context.fixtureRefreshFailed = !!setup.refreshFailed;
  const before = JSON.stringify(candidate);
  const value = JSON.parse(vm.runInContext(`
state.data=fixtureData;state.portfolio=fixturePortfolio;fixturePolicy=fixturePolicyInput;state.refreshFailed=fixtureRefreshFailed;
JSON.stringify(shoppingRecommendation(candidate))
`, context));
  eq(JSON.stringify(context.candidate), before, 'screening does not mutate the supplied stock');
  return value;
}
function expectKey(candidate, key, message, setup) {
  const value = result(candidate, setup);
  eq(value.key, key, message);
  return value;
}
function unavailable(candidate, message, setup) {
  const value = expectKey(candidate, 'unavailable', message, setup);
  eq(value.color, 'orange', message + ': no positive color');
}

const buy = expectKey(stock(), 'buy', 'fresh existing buy tranche is restored');
eq(buy.label, 'Sofort kaufen');
eq(buy.color, 'emerald');
eq(buy.basis, 'Forward-P/E-Regel', 'generic and personal rules stay distinct');
const dip = expectKey(stock({ forward_pe: 27, tranche_key: 'dip' }), 'dip', 'fresh existing dip tranche is restored');
eq(dip.label, 'Bei Rücksetzer nachkaufen');
eq(dip.color, 'sky');
const watch = expectKey(stock({ forward_pe: 35, tranche_key: 'watch' }), 'watch', 'fresh watch tranche is restored');
eq(watch.label, 'Abwarten');
eq(watch.color, 'amber');
// Compatibility: older feeds without a derived tranche still use the existing
// Tech/Value policy rather than losing a valid classification.
for (const [portfolio, multiple, expected] of [
  ['tech', 23, 'buy'], ['tech', 24, 'dip'], ['tech', 29, 'dip'], ['tech', 29.1, 'watch'],
  ['value', 11.3, 'buy'], ['value', 11.31, 'dip'], ['value', 13, 'dip'], ['value', 13.01, 'watch']
]) {
  const candidate = stock({ forward_pe: multiple }); delete candidate.tranche_key;
  expectKey(candidate, expected, portfolio + ' existing policy boundary ' + multiple, { portfolio });
}
const partialData = marketData(); partialData.data_mode = 'partial'; partialData.refresh_batch.status = 'partial';
expectKey(stock(), 'buy', 'a complete stock can be evaluated when another provider is partial', { data: partialData });

for (const [name, mutate, setup] of [
  ['failed reload', () => {}, { refreshFailed: true }],
  ['missing stock batch', candidate => { delete candidate.refresh; }],
  ['different stock batch', candidate => { candidate.refresh.batch_id = 'older-batch'; }],
  ['partial stock', candidate => { candidate.refresh.status = 'partial'; }],
  ['missing required field', candidate => { candidate.refresh.missing_fields = ['rsi14']; }],
  ['missing completion evidence', candidate => { delete candidate.refresh.missing_fields; }],
  ['old quote', candidate => { candidate.as_of = '2026-11-20T21:00:00Z'; }],
  ['future quote', candidate => { candidate.as_of = '2026-11-27T21:00:00Z'; }],
  ['timezone-less quote', candidate => { candidate.as_of = '2026-11-25T21:00:00'; }],
  ['blank currency', candidate => { candidate.currency = ''; }],
  ['zero price', candidate => { candidate.price = 0; }],
  ['loss-making P/E', candidate => { candidate.current_pe = -12; }],
  ['missing current P/E', candidate => { candidate.current_pe = null; }],
  ['missing forward P/E', candidate => { candidate.forward_pe = null; }],
  ['zero forward P/E', candidate => { candidate.forward_pe = 0; }],
  ['nonfinite forward P/E', candidate => { candidate.forward_pe = Infinity; }],
  ['unavailable tranche', candidate => { candidate.tranche_key = 'unavailable'; }]
]) {
  const candidate = stock(); mutate(candidate); unavailable(candidate, name + ' never creates a buy recommendation', setup);
}
for (const mode of ['initial', 'stale']) {
  const data = marketData(); data.data_mode = mode;
  unavailable(stock(), 'feed mode ' + mode + ' blocks positive recommendations', { data });
}
const oldData = marketData();
oldData.refresh_batch = { version: 1, id: '2026-11-25T06:31:00Z', started_at: '2026-11-25T06:31:00Z', completed_at: '2026-11-25T06:32:00Z', status: 'live' };
unavailable(stock(), 'prior daily batch is not current even if the feed calls it live', { data: oldData });
for (const field of ['current_pe', 'forward_pe', 'price', 'currency']) {
  const missingSource = stock(); missingSource.source_status = 'live'; delete missingSource.field_sources[field];
  unavailable(missingSource, field + ' needs explicit live provenance, not an overall-status fallback');
  for (const provenance of ['previous', 'initial', 'unavailable']) {
    const candidate = stock(); candidate.field_sources[field] = provenance;
    unavailable(candidate, field + ' from ' + provenance + ' cannot create a positive badge');
  }
  for (const stamp of [null, 'bad-date', '2026-11-25T06:31:30Z', '2026-11-26T06:33:00Z', '2026-11-26T06:31:30']) {
    const candidate = stock(); candidate.field_updated_at[field] = stamp;
    unavailable(candidate, field + ' requires a timestamp from this completed batch: ' + stamp);
  }
}

for (const candidate of [stock({ tranche_key: 'individual' }), stock({ tranche_policy: 'individual_only' })]) {
  const value = expectKey(candidate, 'individual', 'individually researched rules do not inherit generic portfolio cutoffs');
  eq(value.color, 'slate');
}
eq(expectKey(stock(), 'individual', 'individual-only portfolio remains neutral', { policy: 'individual_only' }).color, 'slate');
const etf = expectKey(stock({ asset_type: 'etf', ticker: 'JEPQ' }), 'etf', 'ETF is evaluated separately');
eq(etf.color, 'slate');
eq(etf.label, 'ETF separat bewerten');
// A freshly evidenced GAAP loss makes corporate P/E economically inapplicable;
// it is not a missing data point and must never inherit a forward-only buy.
const noMeaningfulPE = stock({ ticker: 'NET', current_pe: null, tranche_key: 'buy' });
noMeaningfulPE.field_sources.current_pe = 'not_applicable';
const loss = expectKey(noMeaningfulPE, 'individual', 'fresh not-applicable P/E remains an individual assessment');
eq(loss.color, 'slate');
eq(loss.label, 'Individuell prüfen');
assert.match(loss.reason, /P\/E/); checks++;
unavailable(noMeaningfulPE, 'inapplicable P/E does not bypass an old daily batch', { data: oldData });
unavailable(noMeaningfulPE, 'inapplicable P/E does not bypass a failed reload', { refreshFailed: true });
const partialLoss = structuredClone(noMeaningfulPE); partialLoss.refresh.status = 'partial';
partialLoss.refresh.missing_fields = ['forward_pe'];
unavailable(partialLoss, 'inapplicable P/E does not hide other unconfirmed inputs');

const eligible = expectKey(screenedStock(), 'buy', 'all confirmed personal conditions can trigger a buy despite cached generic watch');
eq(eligible.basis, 'Persönlicher Indikator');
expectKey(screenedStock({ pass: false }), 'watch', 'failed personal condition overrides cached generic buy');
expectKey(screenedStock({ ticker: 'TSM' }), 'buy', 'TSM requires its technical observation date in addition to personal checks');
for (const [name, mutate] of [
  ['missing structured record', candidate => { candidate.buy_indicator = null; }],
  ['unknown version', candidate => { candidate.buy_indicator.version = 2; }],
  ['unavailable evaluation', candidate => { candidate.buy_indicator.status = 'unavailable'; }],
  ['empty conditions', candidate => { candidate.buy_indicator.conditions = []; }],
  ['missing condition', candidate => { candidate.buy_indicator.conditions.pop(); }],
  ['extra condition', candidate => { candidate.buy_indicator.conditions.push({ key: 'invented', actual: 1, threshold: 2, passed: true }); }],
  ['unknown condition', candidate => { candidate.buy_indicator.conditions[0].key = 'invented'; }],
  ['duplicate condition', candidate => { candidate.buy_indicator.conditions[1].key = 'current_pe'; }],
  ['unconfirmed condition', candidate => { candidate.buy_indicator.conditions[0].passed = null; }],
  ['nonboolean pass value', candidate => { candidate.buy_indicator.conditions[0].passed = 1; }],
  ['contradictory pass value', candidate => { candidate.buy_indicator.conditions[0].passed = false; }],
  ['actual differs from snapshot', candidate => { candidate.buy_indicator.conditions[0].actual = 1; }],
  ['SMA threshold differs from snapshot', candidate => { candidate.sma200 = 50; }],
  ['current P/E threshold differs from existing personal policy', candidate => { candidate.buy_indicator.conditions[0].threshold = 18; }],
  ['Forward P/E threshold differs from existing personal policy', candidate => { candidate.buy_indicator.conditions[1].threshold = 21; }],
  ['RSI threshold differs from existing personal policy', candidate => { candidate.buy_indicator.conditions[3].threshold = 60; }],
  ['historical reference is missing', candidate => { candidate.hist_pe = null; }],
  ['historical reference is nonpositive', candidate => { candidate.hist_pe = -1; }],
  ['unknown structured-policy ticker', candidate => { candidate.ticker = 'UNSUPPORTED'; }],
  ['missing threshold', candidate => { candidate.buy_indicator.conditions[0].threshold = null; }],
  ['nonpositive threshold', candidate => { candidate.buy_indicator.conditions[0].threshold = -10; }],
  ['evaluation from yesterday', candidate => { candidate.buy_indicator.evaluated_at = '2026-11-25T06:31:30Z'; }],
  ['evaluation after batch', candidate => { candidate.buy_indicator.evaluated_at = '2026-11-26T06:33:00Z'; }],
  ['timezone-less evaluation', candidate => { candidate.buy_indicator.evaluated_at = '2026-11-26T06:31:30'; }],
  ['different quote date', candidate => { candidate.buy_indicator.as_of = '2026-11-24T21:00:00Z'; }],
  ['currency mismatch', candidate => { candidate.buy_indicator.currency = 'EUR'; }],
  ['unsupported non-USD screen', candidate => { candidate.currency = candidate.buy_indicator.currency = 'EUR'; }],
  ['old SMA field', candidate => { candidate.field_sources.sma200 = 'previous'; }],
  ['old RSI field', candidate => { candidate.field_updated_at.rsi14 = '2026-11-25T06:31:30Z'; }],
  ['invalid SMA', candidate => { candidate.sma200 = null; }],
  ['invalid RSI', candidate => { candidate.rsi14 = 101; }],
  ['invalid negative RSI', candidate => { candidate.rsi14 = -1; }]
]) {
  const candidate = screenedStock(); candidate.tranche_key = 'buy'; mutate(candidate);
  unavailable(candidate, name + ' cannot fall back to a generic positive badge');
}
const contradictoryWatch = screenedStock(); contradictoryWatch.buy_indicator.status = 'watch';
unavailable(contradictoryWatch, 'personal watch inconsistent with every passed condition is rejected');
for (const [index, threshold] of [[0, 26], [1, 21], [3, 50]]) {
  const candidate = screenedStock({ ticker: 'TSM' }); candidate.buy_indicator.conditions[index].threshold = threshold;
  unavailable(candidate, 'TSM absolute policy thresholds cannot be replaced: ' + index);
}
for (const stamp of [null, '2026-11-20T21:00:00Z', '2026-11-27T21:00:00Z', '2026-11-25T21:00:00']) {
  const candidate = screenedStock({ ticker: 'TSM' }); candidate.technicals_as_of = stamp;
  unavailable(candidate, 'TSM technical observation must be valid and recent: ' + stamp);
}

console.log('PASS', checks, 'actual shopping recommendation checks: existing policy, current batch, per-field provenance, personal screen integrity and neutral special cases');
