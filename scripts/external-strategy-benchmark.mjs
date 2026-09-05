import fs from 'node:fs/promises';
import { evaluateBecEmaCrossMarketPhase } from '../production/strategies/bec-ema-cross-market-phase.mjs';
import { evaluateBecDualMomentum } from '../production/strategies/bec-dual-momentum-simple.mjs';
import {
  evaluateBecEmaCross,
  evaluateBecMarketPhases,
  evaluateBecHmaRsiLinreg,
  evaluateBecBullMarketSupportBand,
  evaluateBecWema20,
} from '../production/strategies/bec-builtins-extra.mjs';

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
const HOURLY_BARS = 3000;
const DAILY_BARS = 700;
const WEEKLY_BARS = 220;
const STAKE_USDT = 10;
const BASE_FEE = 0.001;
const BASE_SLIPPAGE = 0.0005;
const API_BASES = ['https://api.binance.com', 'https://api-gcp.binance.com', 'https://data-api.binance.vision'];

const strategies = [
  { id:'bec_ema_cross', source:'jptsantossilva/BEC', evaluate:({currentCloses})=>evaluateBecEmaCross(currentCloses) },
  { id:'bec_ema_cross_with_market_phases', source:'jptsantossilva/BEC', evaluate:({currentCloses})=>evaluateBecEmaCrossMarketPhase(currentCloses) },
  { id:'bec_market_phases', source:'jptsantossilva/BEC', evaluate:({currentCloses})=>evaluateBecMarketPhases(currentCloses) },
  { id:'bec_dual_momentum_simple', source:'jptsantossilva/BEC', evaluate:({currentCloses,dailyCloses,inPosition})=>evaluateBecDualMomentum({currentCloses,dailyCloses,inPosition}) },
  { id:'bec_hma_rsi_linreg', source:'jptsantossilva/BEC', evaluate:({currentCloses})=>evaluateBecHmaRsiLinreg(currentCloses) },
  { id:'bec_bullmarketsupportband', source:'jptsantossilva/BEC', evaluate:({weeklyCloses})=>evaluateBecBullMarketSupportBand(weeklyCloses) },
  { id:'bec_wema20', source:'jptsantossilva/BEC', evaluate:({weeklyCloses})=>evaluateBecWema20(weeklyCloses) },
];

function intervalMs(interval) {
  if (interval === '1h') return 60 * 60 * 1000;
  if (interval === '1d') return 24 * 60 * 60 * 1000;
  if (interval === '1w') return 7 * 24 * 60 * 60 * 1000;
  throw new Error(`Unsupported interval ${interval}`);
}

async function fetchJson(path) {
  let lastError;
  for (const base of API_BASES) {
    try {
      const response = await fetch(`${base}${path}`, { headers: { 'user-agent': 'tst-external-benchmark/1.0' } });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return await response.json();
    } catch (error) { lastError = error; }
  }
  throw lastError || new Error('Binance request failed');
}

async function fetchKlines(symbol, interval, wanted) {
  const step = intervalMs(interval), now = Date.now();
  let startTime = now - (wanted + 10) * step;
  const rows = [];
  while (rows.length < wanted + 10) {
    const limit = Math.min(1000, wanted + 10 - rows.length);
    const batch = await fetchJson(`/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}&startTime=${Math.max(0, Math.floor(startTime))}`);
    if (!Array.isArray(batch) || batch.length === 0) break;
    for (const k of batch) rows.push({ openTime:Number(k[0]), open:Number(k[1]), high:Number(k[2]), low:Number(k[3]), close:Number(k[4]), volume:Number(k[5]), closeTime:Number(k[6]) });
    const last = rows.at(-1); if (!last) break; startTime = last.closeTime + 1; if (batch.length < limit) break;
  }
  return [...new Map(rows.map(r=>[r.openTime,r])).values()].filter(r=>r.closeTime<now).sort((a,b)=>a.openTime-b.openTime).slice(-wanted);
}

function closesAt(rows, closeTime) {
  const out=[]; for(const row of rows){ if(row.closeTime<=closeTime)out.push(row.close); else break; } return out;
}

function simulateStrategy(strategy, hourly, daily, weekly) {
  const trades=[]; let position=null; const closes=hourly.map(r=>r.close);
  for(let i=201;i<hourly.length-1;i++){
    const signal=strategy.evaluate({ currentCloses:closes.slice(0,i+1), dailyCloses:closesAt(daily,hourly[i].closeTime), weeklyCloses:closesAt(weekly,hourly[i].closeTime), inPosition:Boolean(position) });
    const next=hourly[i+1];
    if(!position&&signal.action==='BUY_SIGNAL'){
      position={signalIndex:i,entryIndex:i+1,entryTime:next.openTime,entryPrice:next.open,entryReason:signal.reason}; continue;
    }
    if(position&&signal.action==='SELL_SIGNAL'){
      trades.push({...position,exitSignalIndex:i,exitIndex:i+1,exitTime:next.openTime,exitPrice:next.open,exitReason:signal.reason}); position=null;
    }
  }
  if(position){const last=hourly.at(-1);trades.push({...position,exitSignalIndex:hourly.length-1,exitIndex:hourly.length-1,exitTime:last.closeTime,exitPrice:last.close,exitReason:'END_OF_SAMPLE_MARK'});}
  return trades;
}

function tradeNetReturn(trade, fee, slippage) {
  const entry=trade.entryPrice*(1+slippage), exit=trade.exitPrice*(1-slippage);
  return (exit*(1-fee))/(entry*(1+fee))-1;
}
function metrics(trades, fee=BASE_FEE, slippage=BASE_SLIPPAGE) {
  const pnls=trades.map(t=>STAKE_USDT*tradeNetReturn(t,fee,slippage)); const wins=pnls.filter(x=>x>0),losses=pnls.filter(x=>x<0);
  const gp=wins.reduce((a,b)=>a+b,0),gl=losses.reduce((a,b)=>a+b,0),pf=gl<0?gp/Math.abs(gl):(gp>0?999:0),exp=pnls.length?pnls.reduce((a,b)=>a+b,0)/pnls.length:0,wr=pnls.length?wins.length/pnls.length:0;
  let eq=0,peak=0,dd=0; for(const pnl of pnls){eq+=pnl;peak=Math.max(peak,eq);dd=Math.max(dd,peak-eq);}
  const net=pnls.reduce((a,b)=>a+b,0);
  return {trades:trades.length,netPnlUSDT:Number(net.toFixed(6)),netReturnOn50Pct:Number((net/50*100).toFixed(4)),profitFactor:Number(pf.toFixed(4)),expectancyUSDT:Number(exp.toFixed(6)),winRatePct:Number((wr*100).toFixed(2)),maxDrawdownUSDT:Number(dd.toFixed(6))};
}
function aggregateTradeSets(sets){return sets.flat().sort((a,b)=>a.entryTime-b.entryTime);}
function promotionDecision(allMetrics,oosMetrics,stressMetrics){const reasons=[];if(allMetrics.trades<20)reasons.push('TOO_FEW_TRADES');if(allMetrics.profitFactor<1.25)reasons.push('PF_BELOW_1_25');if(allMetrics.expectancyUSDT<=0)reasons.push('EXPECTANCY_NOT_POSITIVE');if(allMetrics.maxDrawdownUSDT>5)reasons.push('DRAWDOWN_ABOVE_10PCT_CAPITAL');if(oosMetrics.trades<5)reasons.push('OOS_TOO_FEW_TRADES');if(oosMetrics.profitFactor<1.10)reasons.push('OOS_PF_BELOW_1_10');if(oosMetrics.expectancyUSDT<=0)reasons.push('OOS_EXPECTANCY_NOT_POSITIVE');if(stressMetrics.profitFactor<1.0)reasons.push('STRESS_PF_BELOW_1');if(stressMetrics.expectancyUSDT<=0)reasons.push('STRESS_EXPECTANCY_NOT_POSITIVE');return{promoted:reasons.length===0,reasons};}

async function main(){
  const market={};
  for(const symbol of SYMBOLS){
    const [hourly,daily,weekly]=await Promise.all([fetchKlines(symbol,'1h',HOURLY_BARS),fetchKlines(symbol,'1d',DAILY_BARS),fetchKlines(symbol,'1w',WEEKLY_BARS)]);
    if(hourly.length<1000||daily.length<250||weekly.length<60)throw new Error(`${symbol}: insufficient Binance data`);
    market[symbol]={hourly,daily,weekly}; console.log(`${symbol}: ${hourly.length} hourly / ${daily.length} daily / ${weekly.length} weekly closed candles`);
  }
  const splitTime=Math.min(...SYMBOLS.map(s=>market[s].hourly[Math.floor(market[s].hourly.length*0.70)].openTime)); const results=[];
  for(const strategy of strategies){
    const bySymbol={},sets=[];
    for(const symbol of SYMBOLS){const m=market[symbol],trades=simulateStrategy(strategy,m.hourly,m.daily,m.weekly);bySymbol[symbol]={metrics:metrics(trades),trades:trades.map(t=>({...t,netReturn:Number(tradeNetReturn(t,BASE_FEE,BASE_SLIPPAGE).toFixed(8))}))};sets.push(trades.map(t=>({...t,symbol})));}
    const allTrades=aggregateTradeSets(sets),oosTrades=allTrades.filter(t=>t.entryTime>=splitTime),all=metrics(allTrades),oos=metrics(oosTrades),stress=metrics(allTrades,BASE_FEE*2,BASE_SLIPPAGE*2),promotion=promotionDecision(all,oos,stress);
    results.push({id:strategy.id,source:strategy.source,validated:promotion.promoted,qualityScore:promotion.promoted?Number(Math.min(1,(all.profitFactor/2)*0.5+(oos.profitFactor/2)*0.3+(stress.profitFactor/2)*0.2).toFixed(4)):0,all,oos,stress,promotion,bySymbol});
  }
  const report={schemaVersion:2,generatedAt:new Date().toISOString(),source:'Binance public closed klines',symbols:SYMBOLS,timeframe:'1h',higherTimeframes:['1d','1w'],execution:'signal_on_closed_bar__next_bar_open',fees:{perSide:BASE_FEE,slippagePerSide:BASE_SLIPPAGE,stressMultiplier:2},stakeUSDT:STAKE_USDT,assumedCapitalUSDT:50,oosSplitPct:30,liveReady:false,results};
  await fs.mkdir('validation/external',{recursive:true});await fs.writeFile('validation/external/benchmark-latest.json',JSON.stringify(report,null,2));console.log(JSON.stringify({generatedAt:report.generatedAt,results:results.map(r=>({id:r.id,all:r.all,oos:r.oos,stress:r.stress,promotion:r.promotion}))},null,2));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
