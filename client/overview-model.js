/* Pure aggregation of independent FutureModel results. No DOM or global state. */
function aggregateOverview(entries, years) {
  const fail = (message, ids=[]) => ({error:message,code:'overview',invalidPortfolios:ids});
  if(!Number.isInteger(years)||years<1||years>30)return fail('Bitte Zeitraum anpassen');
  if(!Array.isArray(entries)||entries.length>22)return fail('Bitte Depots anpassen');
  // Exclusion is applied before validating inputs: a deliberately excluded
  // incomplete scenario must not prevent the included depots being calculated.
  entries=entries.filter(entry=>entry?.includeInOverview!==false&&entry?.settings?.includeInOverview!==false);
  const required=['portfolio','contributions','cash','wealth','dividendsCumulative','dividendsNetCumulative','dividendTaxesCumulative','dividendsAnnualRunRate','dividendsAnnualRunRateNet','annualDividendInitial','annualDividendContributions','annualDividendReinvested','annualDividendInitialNet','annualDividendContributionsNet','annualDividendReinvestedNet','last12MonthsDividends','last12MonthsDividendsNet'];
  const dividendFields=required.filter(key=>!['portfolio','contributions','cash','wealth'].includes(key));
  const usesDividends=entry=>entry.dividends===undefined?entry.id!=='tech':entry.dividends===true;
  const growthRow=row=>({...row,...Object.fromEntries(dividendFields.map(key=>[key,0])),cash:0,wealth:row.portfolio});
  const ids=new Set();
  for(const entry of entries){
    if(!entry||typeof entry.id!=='string'||!entry.id.trim()||ids.has(entry.id)||entry.dividends!==undefined&&typeof entry.dividends!=='boolean')return fail('Bitte Depots anpassen');
    ids.add(entry.id);
    if(!entry.result||entry.result.error)return fail('Bitte '+(entry.name||entry.id)+' anpassen',[entry.id]);
    if(!Number.isFinite(entry.result.initial)||entry.result.initial<0||!Number.isFinite(entry.result.monthly)||entry.result.monthly<0)return fail('Bitte '+(entry.name||entry.id)+' anpassen',[entry.id]);
    if(!Array.isArray(entry.result.series)||entry.result.series.length!==years*12+1||entry.result.years!==undefined&&entry.result.years!==years)return fail('Bitte Zeitraum für '+(entry.name||entry.id)+' anpassen',[entry.id]);
    if(!entry.result.final||!Array.isArray(entry.result.final.positions))return fail('Bitte '+(entry.name||entry.id)+' anpassen',[entry.id]);
    if(entry.result.final.positions.some(position=>!position||typeof position.id!=='string'||!position.id.trim()||!Number.isFinite(position.weight)||position.weight<0))return fail('Bitte Positionen für '+(entry.name||entry.id)+' anpassen',[entry.id]);
    if((entry.result.initial>0||entry.result.monthly>0)&&!entry.result.final.positions.length)return fail('Bitte Positionen für '+(entry.name||entry.id)+' anpassen',[entry.id]);
    if(usesDividends(entry)&&(!Number.isFinite(entry.result.initialDividendGrossAnnual)||!Number.isFinite(entry.result.initialDividendNetAnnual)))return fail('Bitte '+(entry.name||entry.id)+' anpassen',[entry.id]);
  }
  // Every model must be recomputed for the chosen common horizon; no extension
  // by repeating their last value, and no silent truncation of the longer one.
  const maps=entries.map(entry=>new Map(entry.result.series.map(row=>[row?.month,usesDividends(entry)?row:row&&growthRow(row)])));
  for(let index=0;index<entries.length;index++)for(let month=0;month<=years*12;month++){
    const row=maps[index].get(month);
    if(!row||required.some(key=>!Number.isFinite(row[key])))return fail('Bitte Zeitraum für '+(entries[index].name||entries[index].id)+' anpassen',[entries[index].id]);
  }
  const initial=entries.reduce((sum,e)=>sum+e.result.initial,0),monthly=entries.reduce((sum,e)=>sum+e.result.monthly,0);
  const series=Array.from({length:years*12+1},(_,month)=>{
    const parts=entries.map((entry,index)=>({...maps[index].get(month),id:entry.id,name:entry.name||entry.id}));
    const row={month,year:month/12,parts};
    for(const key of required)row[key]=parts.reduce((sum,p)=>sum+p[key],0);
    return row;
  });
  if(!Number.isFinite(initial)||!Number.isFinite(monthly)||series.some(row=>required.some(key=>!Number.isFinite(row[key]))))return fail('Bitte kleinere Depotwerte anpassen');
  const final={...series.at(-1),gain:series.at(-1).wealth-series.at(-1).contributions};
  const annual=series.filter(row=>row.month>0&&row.month%12===0).map(row=>{
    const previous=series[row.month-12],paidIn=row.contributions-previous.contributions;
    return {...row,paidIn,annualPortfolioGain:row.portfolio-previous.portfolio-paidIn,annualGain:row.wealth-previous.wealth-paidIn};
  });
  const weightFor=e=>initial>0?e.result.initial:monthly>0?e.result.monthly:0,denominator=initial||monthly;
  const rate=(entry,key)=>key==='weightedYieldPct'&&!usesDividends(entry)?0:entry.result[key];
  const weighted=key=>denominator>0&&entries.every(e=>weightFor(e)===0||Number.isFinite(rate(e,key)))?entries.reduce((sum,e)=>sum+(weightFor(e)===0?0:weightFor(e)/denominator*rate(e,key)),0):null;
  const dividendEntries=entries.filter(usesDividends),modes=new Set(dividendEntries.map(e=>e.result.reinvest));
  const positionDividendFields=['dividendsCumulative','dividendsNetCumulative','dividendTaxesCumulative','cash','dividendGrowthPct','last12MonthsDividends','last12MonthsDividendsNet','annualDividendRunRate','annualDividendRunRateNet'];
  final.positions=entries.flatMap(entry=>entry.result.final.positions.map(position=>({...position,...(!usesDividends(entry)?Object.fromEntries(positionDividendFields.map(key=>[key,0])):{}),id:entry.id+':'+position.id,portfolioId:entry.id,originalId:position.id,weight:denominator>0?position.weight*(weightFor(entry)/denominator):0})));
  const weightedDividendGrowthPct=denominator>0&&final.positions.every(p=>Number.isFinite(p.dividendGrowthPct))?final.positions.reduce((sum,p)=>sum+p.weight*p.dividendGrowthPct,0):null;
  return {initial,monthly,years,series,annual,final,empty:entries.length===0,
    initialDividendGrossAnnual:dividendEntries.reduce((sum,e)=>sum+e.result.initialDividendGrossAnnual,0),
    initialDividendNetAnnual:dividendEntries.reduce((sum,e)=>sum+e.result.initialDividendNetAnnual,0),
    weightedReturnPct:weighted('weightedReturnPct'),weightedYieldPct:weighted('weightedYieldPct'),weightedDividendGrowthPct,
    scenarioCAGR:initial>0&&monthly===0?(Math.pow(final.wealth/initial,1/years)-1)*100:null,
    reinvestMode:modes.size===0?'payout':modes.size===1?(dividendEntries[0].result.reinvest?'reinvest':'payout'):'mixed',
    positionCount:final.positions.length,portfolioCount:entries.length,parts:entries.map(e=>({id:e.id,name:e.name||e.id,result:e.result}))};
}
if(typeof window!=='undefined')window.aggregateOverview=aggregateOverview;
if(typeof module!=='undefined')module.exports={aggregateOverview};
