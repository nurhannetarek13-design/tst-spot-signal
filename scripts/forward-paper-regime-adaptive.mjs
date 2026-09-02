import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { atr, ema, STRATEGY_ID } from '../src/strategies/regime-adaptive-momentum.mjs';

const LEDGER='paper/regime-adaptive-ledger.json';
const SCAN='artifacts/paper-regime-adaptive.json';
const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
const CFG={startingEquity:20.12,reserve:2,maxPositions:2,maxPositionUSDT:7,maxRiskUSDT:0.20,roundTripCostPct:0.36,dailyLossLimitUSDT:0.50,atrStopMult:2.5,maxHoldBars:120};

async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return r.json();last=new Error(`${b} ${r.status}`)}catch(e){last=e}}throw last||new Error('NO_MARKET_DATA')}
async function candles(symbol,limit=260){const x=await get(`/api/v3/klines?symbol=${symbol}&interval=4h&limit=${limit}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10]}));}
const round=(x,n=8)=>Number(Number(x).toFixed(n));
const dayKey=ms=>new Date(ms).toISOString().slice(0,10);

if(!fs.existsSync(LEDGER)) throw new Error('MISSING_LEDGER');
const state=JSON.parse(fs.readFileSync(LEDGER,'utf8'));
if(state.strategy!==STRATEGY_ID) throw new Error('STRATEGY_LEDGER_MISMATCH');

// Produce a fresh read-only market scan first.
execFileSync(process.execPath,['scripts/paper-regime-adaptive.mjs'],{stdio:'inherit'});
const scan=JSON.parse(fs.readFileSync(SCAN,'utf8'));
const now=Date.now();
state.closedTrades ||= [];
state.openPositions ||= [];
state.seenSignals ||= [];

// 1) Reconcile existing paper positions against the newest completed/active 4h candles.
for(const p of [...state.openPositions]){
  const c=await candles(p.symbol,260);
  const closes=c.map(x=>x.c), e50=ema(closes,50), a=atr(c,14), i=c.length-1, bar=c[i];
  p.highestClose=Math.max(Number(p.highestClose||p.entry),bar.c);
  const dynamicStop=p.highestClose-CFG.atrStopMult*a[i];
  p.stop=Math.max(Number(p.stop),dynamicStop);
  p.barsHeld=Number(p.barsHeld||0)+1;
  let exitPrice=null, exitReason=null;
  if(bar.l<=p.stop){exitPrice=p.stop;exitReason='ATR_TRAILING_STOP';}
  else if(bar.c<e50[i]){exitPrice=bar.c;exitReason='EMA50_TREND_FAILURE';}
  else if(p.barsHeld>=CFG.maxHoldBars){exitPrice=bar.c;exitReason='TIME_EXIT';}
  if(exitPrice!==null){
    const notional=Number(p.qty)*Number(p.entry);
    const gross=Number(p.qty)*(exitPrice-Number(p.entry));
    const costs=notional*(CFG.roundTripCostPct/100);
    const net=gross-costs;
    state.cashUSDT=Number(state.cashUSDT)+Number(p.qty)*exitPrice;
    state.realizedPnLUSDT=Number(state.realizedPnLUSDT||0)+net;
    state.closedTrades.push({symbol:p.symbol,entry:p.entry,exit:round(exitPrice),qty:p.qty,openedAt:p.openedAt,closedAt:new Date(now).toISOString(),reason:exitReason,grossPnLUSDT:round(gross),costsUSDT:round(costs),netPnLUSDT:round(net),barsHeld:p.barsHeld,strategy:STRATEGY_ID});
    state.openPositions=state.openPositions.filter(x=>x.symbol!==p.symbol);
  }
}

// 2) Conservative daily loss gate on realized paper PnL.
const today=dayKey(now);
const todayPnL=state.closedTrades.filter(t=>String(t.closedAt||'').startsWith(today)).reduce((s,t)=>s+Number(t.netPnLUSDT||0),0);
const dailyGateOpen=todayPnL>-CFG.dailyLossLimitUSDT;

// 3) Open at most two globally concurrent positions from the fresh ranked scan.
if(dailyGateOpen){
  for(const cand of scan.candidates||[]){
    if(state.openPositions.length>=CFG.maxPositions) break;
    if(state.openPositions.some(p=>p.symbol===cand.symbol)) continue;
    const c=await candles(cand.symbol,260), bar=c[c.length-1];
    const signalKey=`${cand.symbol}:${bar.t}`;
    if(state.seenSignals.includes(signalKey)) continue;
    const spend=Math.min(Number(cand.notional||0),CFG.maxPositionUSDT,Math.max(0,Number(state.cashUSDT)-CFG.reserve));
    if(!(spend>0)) continue;
    const qty=Math.min(Number(cand.qty||0),spend/Number(cand.entry));
    const actualSpend=qty*Number(cand.entry);
    const risk=qty*Math.max(0,Number(cand.entry)-Number(cand.stop));
    if(!(qty>0) || risk>CFG.maxRiskUSDT+1e-9) continue;
    state.cashUSDT=Number(state.cashUSDT)-actualSpend;
    state.openPositions.push({symbol:cand.symbol,entry:round(cand.entry),stop:round(cand.stop),qty:round(qty,12),notionalUSDT:round(actualSpend),riskUSDT:round(risk),score:cand.score,openedAt:new Date(now).toISOString(),signalBarTime:new Date(bar.t).toISOString(),highestClose:round(bar.c),barsHeld:0,strategy:STRATEGY_ID,mode:'PAPER_ONLY'});
    state.seenSignals.push(signalKey);
  }
}

state.seenSignals=state.seenSignals.slice(-500);
state.lastRunAt=new Date(now).toISOString();
state.liveTrading=false;
state.config=CFG;
state.todayRealizedPnLUSDT=round(todayPnL);
state.dailyLossGateOpen=dailyGateOpen;
state.openExposureUSDT=round(state.openPositions.reduce((s,p)=>s+Number(p.notionalUSDT||0),0));
state.realizedPnLUSDT=round(state.realizedPnLUSDT||0);
state.cashUSDT=round(state.cashUSDT);
state.closedTradeCount=state.closedTrades.length;
state.winCount=state.closedTrades.filter(t=>Number(t.netPnLUSDT)>0).length;
state.lossCount=state.closedTrades.filter(t=>Number(t.netPnLUSDT)<0).length;
state.winRate=state.closedTrades.length?round(state.winCount/state.closedTrades.length,4):null;

fs.writeFileSync(LEDGER,JSON.stringify(state,null,2)+'\n');
fs.mkdirSync('artifacts',{recursive:true});
fs.writeFileSync('artifacts/forward-paper-regime-adaptive.json',JSON.stringify(state,null,2)+'\n');
console.log(JSON.stringify({ok:true,strategy:STRATEGY_ID,liveTrading:false,openPositions:state.openPositions.length,closedTrades:state.closedTradeCount,realizedPnLUSDT:state.realizedPnLUSDT,cashUSDT:state.cashUSDT,dailyLossGateOpen:state.dailyLossGateOpen},null,2));
