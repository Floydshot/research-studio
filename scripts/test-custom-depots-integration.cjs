const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Run the real application functions and pure projection model without loading
// a browser, making HTTP requests, or touching any user's saved project state.
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const overview = fs.readFileSync(path.join(root, 'client/overview-model.js'), 'utf8');
function declaration(name) {
  const marker = '    function ' + name + '(';
  const start = html.indexOf(marker);
  assert.ok(start >= 0, 'actual application function exists: ' + name);
  const remainder = html.slice(start + marker.length);
  const end = /\n    (?:(?:async )?function |(?:const|let) [A-Za-z_$])/.exec(remainder);
  assert.ok(end, 'stable source boundary exists: ' + name);
  return html.slice(start, start + marker.length + end.index);
}
function constant(name) {
  const match = html.match(new RegExp('^    const ' + name + ' = [^\\n]+', 'm'));
  assert.ok(match, 'actual application constant exists: ' + name);
  return match[0];
}
const modelStart = html.indexOf('const FutureModel = (() => {');
const modelEnd = html.indexOf('\nconst PortfolioInput = ', modelStart);
assert.ok(modelStart >= 0 && modelEnd > modelStart);
const functions = [
  'projectDepotIds', 'basePortfolio', 'portfolio', 'httpsSource',
  'futureUsesDividends', 'futureModelsDividends', 'futureValidInput',
  'futureInputIssues', 'futureInputMessage', 'futureDefaults', 'futureSettings',
  'projectSettingsSnapshot', 'saveFutureSettings', 'setFutureHorizon',
  'loadFutureSettings', 'createCustomDepot', 'futureCatalog', 'futureScenarioStocks',
  'futureFx', 'futureStockAssumptions', 'emptyProjectDepot', 'calculateProjectDepot',
  'resetProjectView'
];

const templateData = {
  portfolios: [
    { id: 'tech', name: 'Tech Stonks', cluster_order: ['Big Tech'], stocks: [
      { ticker: 'MSFT', name: 'Microsoft', cluster: 'Big Tech', price: 120, currency: 'USD' },
      { ticker: 'AAPL', name: 'Apple', cluster: 'Big Tech', price: 80, currency: 'USD' }
    ] },
    { id: 'value', name: 'Value Stonks', cluster_order: ['Versicherer'], stocks: [
      { ticker: 'ALV.DE', name: 'Allianz', cluster: 'Versicherer', price: 200, currency: 'EUR',
        dividend_annual: 10, dividend_growth: { status: 'confirmed', currency: 'EUR', projection_rate_pct: 8 },
        field_sources: { div_yield: 'live' }, div_yield: 5 }
    ] },
    { id: 'classic', name: 'A24', cluster_order: ['Classic'], stocks: [] }
  ],
  security_universe: { stocks: [
    { ticker: 'TTE.PA', name: 'TotalEnergies', cluster: 'Energy', price: 60, currency: 'EUR',
      dividend_annual: 4, dividend_growth: { status: 'confirmed', currency: 'EUR', projection_rate_pct: 4.5 },
      field_sources: { div_yield: 'live' }, div_yield: 4 / 60 * 100 }
  ] },
  fx_rates: { base: 'EUR', rates: { USD: 1.2 }, source_url: 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml',
    as_of: '2026-11-25', status: 'live' }
};
const growthId = 'depot-11111111-1234-4567-89ab-0123456789ab';
const dividendId = 'depot-22222222-1234-4567-89ab-0123456789ab';
const emptyId = 'depot-33333333-1234-4567-89ab-0123456789ab';
const baseScenario = { schemaVersion: 2, allocationMode: 'weights', initial: 10000, monthly: 50,
  source: 'manual', manual: 7, years: 5, extend: true, reinvest: true, migrated: true,
  includeInOverview: true, excluded: {}, removed: {}, weights: {}, shares: {}, returnOverrides: {}, extraTickers: [] };
const saved = {
  tech: { ...baseScenario, allocationMode: 'shares', includeInOverview: false, years: 5,
    shares: { MSFT: 10, AAPL: 5 }, returnOverrides: { MSFT: 11.5 }, removed: { AAPL: true } },
  value: { ...baseScenario, years: 20, weights: { 'ALV.DE': 100 }, initial: 25000 },
  overview: { years: 15 },
  customDepots: [
    { id: growthId, name: 'Private Growth', dividends: false,
      settings: { ...baseScenario, allocationMode: 'shares', includeInOverview: false,
        shares: { MSFT: 2.5, 'TTE.PA': 10 }, returnOverrides: { MSFT: 12, 'TTE.PA': 14 },
        extraTickers: ['MSFT', 'TTE.PA'], years: 10 } },
    { id: dividendId, name: 'Mein Einkommen', dividends: true,
      settings: { ...baseScenario, initial: 12000, monthly: 100, reinvest: false,
        weights: { 'TTE.PA': 100 }, extraTickers: ['TTE.PA'] } }
  ]
};

let checks = 0;
const eq = (actual, expected, message) => { assert.deepEqual(actual, expected, message); checks++; };
const near = (actual, expected, message) => { assert.ok(Number.isFinite(actual) && Math.abs(actual - expected) < 1e-8, message); checks++; };
const nodes = new Map();
const removedTabs = [], closedDialogs = [], destroyedCharts = [], resetModules = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id, { id, hidden: false, value: 'old private input', textContent: '',
    replaceChildren() { this.cleared = true; }, remove() { removedTabs.push(id); }, close() { closedDialogs.push(id); } });
  return nodes.get(id);
}
const writes = [], openWrites = [], storage = new Map();
let generated = 0;
const context = vm.createContext({
  console, URL, Intl, structuredClone,
  crypto: { randomUUID: () => (++generated).toString(16).padStart(8, '0') + '-aaaa-4aaa-8aaa-aaaaaaaaaaaa' },
  document: { querySelectorAll(selector) {
    if (selector === '[data-custom-depot]') return [node('old-custom-tab')];
    if (selector.startsWith('dialog[open]')) return [node('old-modal')];
    return [];
  } },
  $: node,
  localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => { storage.set(key, value); openWrites.push({ key, value }); } },
  window: { ProjectWorkspaces: { id: 'floyd', private: true, unlocked: true,
    save(value) { writes.push({ project: context.window.ProjectWorkspaces.id, settings: structuredClone(value) }); },
    flush: () => Promise.resolve() },
    StockRequests: { reset: () => resetModules.push('requests') }, ProjectImport: { reset: () => resetModules.push('import') },
    ProjectRemoval: { reset: () => resetModules.push('removal') }, CustomDepots: { reset: () => resetModules.push('depots') } },
  syncStudioView() {}, syncProjectPanels() {}, renderFilters() {}, render() {},
  fixture: structuredClone(templateData), supplied: structuredClone(saved)
});
const declarations = [constant('number'), constant('positive'), constant('futureNumber'), constant('FUTURE_STORAGE'), constant('futureStudio')].join('\n');
vm.runInContext(`
const state={data:fixture,projectReady:true,portfolio:'tech',view:'future',overview:false,cluster:'Alle',search:'',selected:null,chart:null};
const returnsSort={tech:{key:'category'},value:{key:'category'}};
const overviewState={charts:[],request:0};
${declarations}
${html.slice(modelStart, modelEnd)}
${overview}
${functions.map(declaration).join('\n')}
`, context);
const run = source => vm.runInContext(source, context);
const json = source => JSON.parse(run('JSON.stringify(' + source + ')'));
const beforePublic = json('state.data');

run('loadFutureSettings(supplied)');
eq(json('projectDepotIds()'), ['tech', 'value', growthId, dividendId]);
eq(json('futureStudio.customDepots'), saved.customDepots.map(({ settings, ...metadata }) => metadata));
eq(run('futureSettings("tech").includeInOverview'), false);
eq(run(`futureSettings('${growthId}').includeInOverview`), false);
eq(json(`futureSettings('${growthId}').shares`), saved.customDepots[0].settings.shares);
eq(json(`futureSettings('${growthId}').returnOverrides`), saved.customDepots[0].settings.returnOverrides);
eq(run(`futureSettings('${dividendId}').reinvest`), false);
eq(run(`futureModelsDividends('${growthId}')`), false);
eq(run(`futureModelsDividends('${dividendId}')`), true);
for (const id of ['tech', 'value', growthId, dividendId]) eq(run(`futureSettings('${id}').years`), 15, 'shared restored horizon');
const snapshot = json('projectSettingsSnapshot()');
eq(snapshot.tech.shares, saved.tech.shares, 'base shares preserved');
eq(snapshot.tech.returnOverrides, saved.tech.returnOverrides, 'base overrides preserved');
eq(snapshot.customDepots[0].settings.shares, saved.customDepots[0].settings.shares);
eq(snapshot.customDepots[0].settings.includeInOverview, false);
eq(snapshot.customDepots[1].settings.reinvest, false);
eq(snapshot.customDepots[0].name, 'Private Growth');
// Use the original serialized snapshot for an actual fresh-session round trip.
context.roundTrip = structuredClone(snapshot);
run('futureStudio.settings={};loadFutureSettings(roundTrip)');
eq(json('projectSettingsSnapshot()'), snapshot, 'save/load snapshot round-trip is lossless');
run(`futureSettings('${growthId}').shares.MSFT=99`);
eq(snapshot.customDepots[0].settings.shares.MSFT, 2.5, 'snapshot is detached from mutable inputs');
run('futureStudio.settings={};loadFutureSettings(roundTrip)');
run('setFutureHorizon(30)');
for (const id of ['tech', 'value', growthId, dividendId]) eq(run(`futureSettings('${id}').years`), 30, 'horizon updates every depot');
eq(writes.at(-1).project, 'floyd');
eq(writes.at(-1).settings.overview.years, 30);
eq(writes.at(-1).settings.customDepots.length, 2);
eq(run('setFutureHorizon(13)'), false);

const canonical = run(`futureScenarioStocks(futureSettings('${growthId}'),'${growthId}')[0]===futureCatalog().find(stock=>stock.ticker==='MSFT')`);
eq(canonical, true, 'custom positions resolve canonical catalog objects');
eq(json(`futureScenarioStocks(futureSettings('${growthId}'),'${growthId}').map(stock=>stock.ticker)`), ['MSFT', 'TTE.PA']);
run(`futureSettings('${growthId}').removed.MSFT=true`);
eq(json(`futureScenarioStocks(futureSettings('${growthId}'),'${growthId}').map(stock=>stock.ticker)`), ['TTE.PA']);
eq(json('futureScenarioStocks(futureSettings("tech"),"tech").map(stock=>stock.ticker)'), ['MSFT'], 'base membership removal still applies');
eq(json('state.data'), beforePublic, 'scenario metadata never mutates public market feed');
run(`delete futureSettings('${growthId}').removed.MSFT`);
const growthAssumption = json(`futureStockAssumptions(futureCatalog().find(stock=>stock.ticker==='TTE.PA'),futureSettings('${growthId}'),'${growthId}')`);
eq(growthAssumption.totalReturnPct, 14, 'card override wins over manual base');
eq(growthAssumption.dividendYieldPct, 0, 'growth depot excludes dividend yield');
eq(growthAssumption.dividendGrowthPct, 0, 'growth depot excludes dividend growth');
eq(growthAssumption.problem, null);
near(growthAssumption.valueEUR, 600);
near(growthAssumption.weight, 600);
run(`futureSettings('${growthId}').shares['TTE.PA']=null`);
const unfinishedQuantity = json(`futureStockAssumptions(futureCatalog().find(stock=>stock.ticker==='TTE.PA'),futureSettings('${growthId}'),'${growthId}')`);
eq(unfinishedQuantity.weight, null, 'unfinished shares never become a finite capital weight');
eq(unfinishedQuantity.problemField, 'quantity');
assert.match(run(`calculateProjectDepot('${growthId}',15).result.error`), /Shares/); checks++;
run(`futureSettings('${growthId}').shares['TTE.PA']=10`);
const dividendAssumption = json(`futureStockAssumptions(futureCatalog().find(stock=>stock.ticker==='TTE.PA'),futureSettings('${dividendId}'),'${dividendId}')`);
near(dividendAssumption.dividendYieldPct, 4 / 60 * 100);
near(dividendAssumption.dividendGrowthPct, 4.5);
eq(dividendAssumption.problem, null);
const growthEntry = json(`calculateProjectDepot('${growthId}',15)`);
eq(growthEntry.dividends, false);
eq(growthEntry.includeInOverview, false);
eq(growthEntry.result.error, undefined);
near(growthEntry.result.initial, 850);
near(growthEntry.result.final.cash, 0);
near(growthEntry.result.final.dividendsCumulative, 0);
const dividendEntry = json(`calculateProjectDepot('${dividendId}',15)`);
eq(dividendEntry.dividends, true);
eq(dividendEntry.result.error, undefined);
assert.ok(dividendEntry.result.final.last12MonthsDividendsNet > 0); checks++;

run(`futureStudio.customDepots.push({id:'${emptyId}',name:'Empty new',dividends:true});
futureStudio.settings['${emptyId}']={...futureDefaults(),initial:0,monthly:0,years:15,migrated:true};`);
const empty = json(`calculateProjectDepot('${emptyId}',15)`);
eq(empty.result.error, undefined, 'empty zero-value custom depot is valid');
near(empty.result.initial, 0);
near(empty.result.final.portfolio, 0);
near(empty.result.final.last12MonthsDividendsNet, 0);
eq(empty.result.series.length, 181);
eq(run(`window.aggregateOverview([calculateProjectDepot('${dividendId}',15),calculateProjectDepot('${emptyId}',15)],15).error`), undefined, 'empty custom depot does not block Overview');
const withEmpty = json(`window.aggregateOverview([calculateProjectDepot('${dividendId}',15),calculateProjectDepot('${emptyId}',15)],15)`);
near(withEmpty.final.portfolio, dividendEntry.result.final.portfolio);
run(`futureSettings('${emptyId}').initial=1000`);
assert.match(run(`calculateProjectDepot('${emptyId}',15).result.error`), /Positionen/); checks++;
run(`futureSettings('${emptyId}').initial=0;futureSettings('${emptyId}').monthly=100`);
assert.match(run(`calculateProjectDepot('${emptyId}',15).result.error`), /Positionen/); checks++;
run(`futureSettings('${emptyId}').monthly=0`);

const beforeCreateSaves = writes.length;
run(`createCustomDepot({name:' Neues Wachstum ',dividends:false},{projectId:'floyd',isCurrent:()=>true})`);
const newId = run('state.portfolio');
assert.match(newId, /^depot-[0-9a-f-]+$/); checks++;
eq(run(`futureScenarioStocks(futureSettings('${newId}'),'${newId}').length`), 0, 'new depot starts empty');
eq(run(`futureSettings('${newId}').initial`), 0, 'new depot never invents starting capital');
eq(run(`futureSettings('${newId}').monthly`), 0);
eq(run('state.view'), 'future');
eq(run(`futureModelsDividends('${newId}')`), false);
eq(run(`futureSettings('${newId}').years`), 30);
eq(writes.length, beforeCreateSaves + 1);
eq(writes.at(-1).project, 'floyd');
eq(writes.at(-1).settings.customDepots.at(-1).name, 'Neues Wachstum');
const createdCount = run('futureStudio.customDepots.length');
for (const expression of [
  `createCustomDepot({name:'Bad',dividends:true},{projectId:'ts',isCurrent:()=>true})`,
  `createCustomDepot({name:'Bad',dividends:true},{projectId:'floyd',isCurrent:()=>false})`,
  `createCustomDepot({name:'Neues Wachstum',dividends:true},{projectId:'floyd',isCurrent:()=>true})`
]) { assert.throws(() => run(expression)); checks++; }
eq(run('futureStudio.customDepots.length'), createdCount, 'invalid create never changes collection');

run(`returnsSort['${growthId}']={key:'name'};state.chart={destroy:()=>destroyedCharts.push('research')};`);
// A host-side spy is needed only for this reset callback; all reset logic is real.
context.destroyedCharts = destroyedCharts;
run('resetProjectView()');
eq(json('futureStudio.customDepots'), []);
eq(json('futureStudio.settings'), {});
eq(run('state.projectReady'), false);
eq(run('state.portfolio'), 'tech');
eq(run(`returnsSort['${growthId}']`), undefined);
eq(destroyedCharts, ['research']);
eq(removedTabs.includes('old-custom-tab'), true);
eq(closedDialogs.includes('old-modal'), true);
eq(resetModules.includes('depots'), true);

context.window.ProjectWorkspaces.id = 'ts';
run('loadFutureSettings({tech:{schemaVersion:2},value:{schemaVersion:2},overview:{years:10}})');
eq(json('projectDepotIds()'), ['tech', 'value'], 'another private project does not inherit Floyd depots');
eq(json('projectSettingsSnapshot().customDepots'), []);
run('state.projectReady=true;saveFutureSettings()');
eq(writes.at(-1).project, 'ts');
eq(writes.at(-1).settings.customDepots.length, 0);

run('resetProjectView()');
context.window.ProjectWorkspaces.id = 'open'; context.window.ProjectWorkspaces.private = false;
storage.set('research-studio-open-v1', JSON.stringify(saved));
run('loadFutureSettings()');
eq(json('projectDepotIds()'), ['tech', 'value'], 'Open never hydrates private custom metadata');
eq(json('projectSettingsSnapshot().customDepots'), []);
const privateWritesBeforeOpen = writes.length;
run('state.projectReady=true;saveFutureSettings()');
eq(writes.length, privateWritesBeforeOpen, 'Open does not call private state save');
eq(openWrites.length, 1);
eq(JSON.parse(openWrites[0].value).customDepots.length, 0);
assert.throws(() => run(`createCustomDepot({name:'Public attempt',dividends:true},{projectId:'open',isCurrent:()=>true})`)); checks++;
eq(json('state.data'), beforePublic, 'public catalog is unchanged after all create/reset/scenario operations');
console.log('PASS', checks, 'actual custom-depot settings, projection, shared-horizon, empty-state and project-isolation integration checks');
