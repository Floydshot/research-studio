/* Pure model regression checks; no browser, requests or private project data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { aggregateOverview } = require('../client/overview-model.js');

const source = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const start = source.indexOf('const FutureModel =');
const finish = source.indexOf('})();', start) + 5;
assert.ok(start >= 0 && finish > start, 'The actual projection model is available');
const context = vm.createContext({ Date, URL });
vm.runInContext(source.slice(start, finish) + ';this.model=FutureModel;', context);
const plain = value => JSON.parse(JSON.stringify(value));
let checks = 0;
const equal = (actual, expected, message) => { assert.deepEqual(actual, expected, message); checks++; };
const near = (actual, expected, message, tolerance=1e-7) => {
  assert.ok(Number.isFinite(actual) && Math.abs(actual-expected) <= tolerance * Math.max(1, Math.abs(expected)), `${message}: ${actual} / ${expected}`);
  checks++;
};
function depot(id, { dividends=true, years=5, initial=10000, monthly=0, rate=8, yieldPct=4, growth=3, reinvest=false }={}) {
  const result = plain(context.model.project({ positions:[{ id:'SHARED', weight:100, totalReturnPct:rate,
    dividendYieldPct:dividends ? yieldPct : 0, dividendGrowthPct:dividends ? growth : 0 }],
    years, initial, monthly, reinvest:dividends && reinvest, dividendTaxRate:dividends ? .26375 : 0 }));
  assert.ok(!result.error, result.error);
  return { id, name:id, dividends, result };
}

const growth = depot('tech', { dividends:false, initial:20000, monthly:100, rate:10 });
const payout = depot('value', { initial:30000, monthly:200, rate:8, yieldPct:4, reinvest:false });
const reinvest = depot('depot-11111111-1111-4111-8111-111111111111', { initial:50000, monthly:50, rate:7, yieldPct:5, reinvest:true });
const originals = plain([growth, payout, reinvest]);
const result = aggregateOverview([growth, payout, reinvest], 5);
equal(result.error, undefined, 'Three unrelated depots aggregate');
equal(result.portfolioCount, 3, 'Custom depots are counted');
equal(result.positionCount, 3, 'Identical stock IDs in separate depots remain separate positions');
equal(new Set(result.final.positions.map(position=>position.id)).size, 3, 'Position identifiers are namespaced');
near(result.initial, 100000, 'Initial values add');
near(result.monthly, 350, 'Independent savings add');
near(result.weightedReturnPct, 7.9, 'Start-capital weights apply to growth assumptions');
near(result.weightedYieldPct, 3.7, 'Only dividend depots contribute to initial portfolio yield');
near(result.initialDividendGrossAnnual, 3700, 'Initial gross dividends add');
near(result.initialDividendNetAnnual, 3700*.73625, 'Initial net dividends add');
equal(result.reinvestMode, 'mixed', 'Independent dividend modes remain independent');
for (let month=0; month<=60; month++) {
  const row = result.series[month], parts = [growth,payout,reinvest].map(entry=>entry.result.series[month]);
  for (const field of ['portfolio','contributions','cash','wealth','last12MonthsDividends','last12MonthsDividendsNet','dividendsCumulative','annualDividendInitialNet','annualDividendContributionsNet','annualDividendReinvestedNet']) {
    near(row[field], parts.reduce((sum,part)=>sum+part[field],0), `Month ${month}: ${field} is an independent-depot sum`);
  }
}
equal(result.annual.map(row=>row.year), [1,2,3,4,5], 'Annual output contains every year');
for (const row of result.annual) {
  const previous = result.series[(row.year-1)*12];
  near(row.paidIn, 4200, `Year ${row.year}: annual savings exclude initial capital`);
  near(row.annualPortfolioGain, row.portfolio-previous.portfolio-row.paidIn, `Year ${row.year}: gains avoid double counting savings`);
  near(row.last12MonthsDividendsNet, payout.result.series[row.month].last12MonthsDividendsNet+reinvest.result.series[row.month].last12MonthsDividendsNet, `Year ${row.year}: payouts are annual, not cumulative`);
}
equal([growth,payout,reinvest], originals, 'Aggregation never mutates source scenarios');

const excluded = { id:'excluded', name:'Broken but excluded', includeInOverview:false, result:{error:'Missing assumptions'} };
const selected = aggregateOverview([growth, payout, excluded], 5);
equal(selected.error, undefined, 'An excluded invalid scenario cannot block included depots');
equal(selected.portfolioCount, 2, 'Excluded depots do not contribute');
near(selected.final.portfolio, growth.result.final.portfolio+payout.result.final.portfolio, 'Excluded amounts are absent');
equal(aggregateOverview([{ ...excluded, includeInOverview:true, settings:{includeInOverview:false} }], 5).portfolioCount, 0, 'Scenario-level false is respected defensively');
equal(aggregateOverview([{ ...excluded, includeInOverview:true }], 5).invalidPortfolios, ['excluded'], 'The same error matters when included');

const empty = aggregateOverview([], 5);
equal(empty.error, undefined, 'No included depots is a valid empty Overview');
equal(empty.empty, true, 'Empty state is explicit');
equal(empty.portfolioCount, 0, 'No included depots have a zero count');
equal(empty.positionCount, 0, 'No positions are invented');
equal(empty.series.length, 61, 'Empty overview retains the requested timeline');
equal(empty.annual.length, 5, 'Empty annual table retains every year');
equal(empty.initial, 0, 'No fake start capital is assigned');
equal(empty.final.portfolio, 0, 'No fake depot value is assigned');
equal(empty.final.last12MonthsDividendsNet, 0, 'No fake dividends are assigned');
equal(empty.weightedReturnPct, null, 'An empty overview has no meaningful weighted return');
equal(empty.reinvestMode, 'payout', 'An empty overview needs no reinvestment mode');
equal(aggregateOverview([{...excluded}],5), empty, 'All depots switched off equals empty');
const withEmpty = aggregateOverview([growth, payout, { id:'empty-custom', dividends:true, result:empty }], 5);
equal(withEmpty.error, undefined, 'An empty custom depot can participate without inventing assets');
near(withEmpty.weightedReturnPct, selected.weightedReturnPct, 'A zero-funded depot does not suppress funded weighted rates');
near(withEmpty.final.portfolio, selected.final.portfolio, 'An empty custom depot adds zero');

const onlyGrowth = aggregateOverview([growth], 5);
equal(onlyGrowth.reinvestMode, 'payout', 'Growth-only overview has no reinvested dividends');
equal(onlyGrowth.initialDividendNetAnnual, 0, 'Growth-only initial dividends are zero');
equal(onlyGrowth.final.last12MonthsDividendsNet, 0, 'Growth-only future dividends are zero');
near(onlyGrowth.final.portfolio, growth.result.final.portfolio, 'Growth model value is preserved');
const noisyGrowth = plain(growth);
noisyGrowth.result.weightedYieldPct = 99;
noisyGrowth.result.initialDividendGrossAnnual = 1000;
noisyGrowth.result.initialDividendNetAnnual = 900;
noisyGrowth.result.reinvest = true;
for (const row of noisyGrowth.result.series) { row.cash=99; row.wealth=row.portfolio+99; row.last12MonthsDividendsNet=5; row.dividendsCumulative=100; }
noisyGrowth.result.final.positions[0].dividendGrowthPct = 100;
const growthSafe = aggregateOverview([noisyGrowth], 5);
equal(growthSafe.initialDividendNetAnnual, 0, 'Growth classification overrides stale dividend summaries');
equal(growthSafe.weightedYieldPct, 0, 'Growth classification overrides stale dividend yield');
equal(growthSafe.final.last12MonthsDividendsNet, 0, 'Growth classification suppresses distribution rows');
equal(growthSafe.final.cash, 0, 'Growth classification suppresses dividend cash');
near(growthSafe.final.wealth, growthSafe.final.portfolio, 'Growth wealth cannot count phantom dividend cash');
equal(growthSafe.reinvestMode, 'payout', 'An irrelevant growth reinvest flag has no effect');
equal(aggregateOverview([{...noisyGrowth, dividends:undefined}],5).final.last12MonthsDividendsNet, 0, 'Legacy Tech Stonks is treated as growth');

const allReinvest = aggregateOverview([growth, reinvest], 5);
equal(allReinvest.reinvestMode, 'reinvest', 'Growth does not turn a dividend reinvestment mode into mixed');
const oldPair = aggregateOverview([{...growth,dividends:undefined},{...payout,dividends:undefined}],5);
near(oldPair.final.portfolio, selected.final.portfolio, 'Existing Tech/Value results are preserved');
near(oldPair.initialDividendNetAnnual, selected.initialDividendNetAnnual, 'Existing Value distributions are preserved');

const longGrowth = depot('growth-long', { dividends:false, years:15, initial:740000, rate:7 });
const longIncome = depot('income-long', { years:15, initial:100000, rate:8, yieldPct:5, reinvest:true });
const long = aggregateOverview([longGrowth,longIncome],15);
equal(long.series.length,181,'A shared 15-year horizon is modeled without repeating a final value');
equal(long.annual.length,15,'A shared 15-year annual table includes all years');
near(long.final.portfolio,longGrowth.result.final.portfolio+longIncome.result.final.portfolio,'Long-horizon growth and income sum');
equal(aggregateOverview([longGrowth],5).invalidPortfolios,['growth-long'],'A longer model cannot be silently truncated');
equal(aggregateOverview([growth],15).invalidPortfolios,['tech'],'A short model cannot be extended silently');
const missingMonth = plain(growth); missingMonth.result.series[10]=missingMonth.result.series[9];
equal(aggregateOverview([missingMonth],5).invalidPortfolios,['tech'],'Missing monthly observations fail closed');
equal(aggregateOverview([growth,{...payout,id:'tech'}],5).code,'overview','Duplicate depot identifiers fail closed');
equal(aggregateOverview([growth],NaN).code,'overview','Invalid years fail closed');
equal(aggregateOverview(null,5).code,'overview','Invalid entry collections fail closed');
const unallocated=plain(growth);unallocated.result.final.positions=[];
equal(aggregateOverview([unallocated],5).invalidPortfolios,['tech'],'Nonzero unallocated capital is not treated as an empty depot');
const invalidPosition=plain(growth);invalidPosition.result.final.positions=[null];
equal(aggregateOverview([invalidPosition],5).invalidPortfolios,['tech'],'Malformed positions fail closed instead of crashing');
invalidPosition.result.final.positions=[{id:'stock',weight:NaN}];
equal(aggregateOverview([invalidPosition],5).invalidPortfolios,['tech'],'Invalid position weights cannot introduce NaN into combined figures');
const monthlyOnly = depot('monthly-only',{dividends:false,initial:0,monthly:500,rate:6});
const monthlyPair = aggregateOverview([monthlyOnly,{id:'zero',dividends:false,result:empty}],5);
near(monthlyPair.weightedReturnPct,6,'Monthly savings define rate weights when initial capital is zero');
console.log(`PASS ${checks} custom-depot Overview checks: independent sums, exclusion, empty state, growth-only dividends, shared horizons and source immutability.`);
