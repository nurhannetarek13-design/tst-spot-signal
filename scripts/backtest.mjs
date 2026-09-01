const BASE = "https://api.binance.com";
const symbols = String(process.env.BACKTEST_SYMBOLS || "BTCUSDT,ETHUSDT,SOLUSDT").split(",").map(x => x.trim().toUpperCase());
const days = Math.max(30, Math.min(730, Number(process.env.BACKTEST_DAYS || 180)));
const fee = Number(process.env.BACKTEST_FEE || 0.001);
const slippage = Number(process.env.BACKTEST_SLIPPAGE || 0.0005);
const capital = Number(process.env.BACKTEST_CAPITAL || 1000);
const riskFraction = Number(process.env.BACKTEST_RISK || 0.01);
const startTime = Date.now() - days * 86400000;

async function klines(symbol, interval) {
  const rows = [];
  let cursor = startTime;
  while (cursor < Date.now()) {
    const url = `${BASE}/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=1000&startTime=${cursor}`;
    const response = await fetch(url, { signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error(`${symbol} ${interval}: HTTP ${response.status}`);
    const page = await response.json();
    if (!page.length) break;
    for (const k of page) rows.push({ t:+k[0], o:+k[1], h:+k[2], l:+k[3], c:+k[4], qv:+k[7], tb:+k[10], closeTime:+k[6] });
    const next = Number(page.at(-1)[6]) + 1;
    if (next <= cursor) break;
    cursor = next;
    if (page.length < 1000) break;
  }
  return rows.filter(x => x.closeTime < Date.now());
}

function ema(values, period) {
  if (values.length < period) return NaN;
  const k = 2 / (period + 1);
  let out = values.slice(0, period).reduce((a,b)=>a+b,0) / period;
  for (let i=period;i<values.length;i++) out = values[i]*k + out*(1-k);
  return out;
}
function median(values) { const s=[...values].sort((a,b)=>a-b), m=Math.floor(s.length/2); return s.length%2?s[m]:(s[m-1]+s[m])/2; }
function rsi(values, period=14) { let g=0,l=0; for(let i=values.length-period;i<values.length;i++){const d=values[i]-values[i-1]; if(d>=0)g+=d;else l-=d;} return l===0?100:100-100/(1+(g/period)/(l/period)); }
function atr(rows, period=14) { const x=[]; for(let i=rows.length-period;i<rows.length;i++){const c=rows[i],p=rows[i-1];x.push(Math.max(c.h-c.l,Math.abs(c.h-p.c),Math.abs(c.l-p.c)));} return x.reduce((a,b)=>a+b,0)/x.length; }
function latestClosed(rows, time) { let lo=0,hi=rows.length-1,ans=-1; while(lo<=hi){const m=(lo+hi)>>1;if(rows[m].closeTime<time){ans=m;lo=m+1;}else hi=m-1;} return ans>=0?rows.slice(0,ans+1):[]; }

function setupAt(rows, i) {
  for(let b=Math.max(25,i-15);b<i;b++){
    const prior=rows.slice(b-20,b), resistance=Math.max(...prior.map(x=>x.h));
    if(rows[b].c<=resistance*1.001 || rows[b].qv<median(prior.map(x=>x.qv))*1.2) continue;
    for(let j=b+1;j<=i;j++){
      const r=rows[j];
      if(r.c<resistance*0.99) break;
      if(r.l<=resistance*1.006 && r.c>=resistance*0.998 && rows[i].c>=resistance && rows[i].c<=resistance*1.08) return { resistance, retest:r };
    }
  }
  return null;
}

function strategySignal(name, c15, i, h1, h4, bh1, bh4) {
  const bar=c15[i], closes=c15.slice(0,i+1).map(x=>x.c), recent=c15.slice(i-20,i), a=atr(c15.slice(0,i+1));
  const trend=h1.at(-1).c>ema(h1.map(x=>x.c),20)&&ema(h1.map(x=>x.c),20)>ema(h1.map(x=>x.c),50)&&h4.at(-1).c>ema(h4.map(x=>x.c),20)&&ema(h4.map(x=>x.c),20)>ema(h4.map(x=>x.c),50);
  const btc=bh1.at(-1).c>ema(bh1.map(x=>x.c),50)&&bh4.at(-1).c>ema(bh4.map(x=>x.c),50);
  const rv=bar.qv/median(recent.map(x=>x.qv)), flow=c15.slice(i-7,i+1), total=flow.reduce((s,x)=>s+x.qv,0), taker=total?flow.reduce((s,x)=>s+x.tb,0)/total:0, momentum=rsi(closes);
  const entry=bar.c*(1+slippage);
  if(name==="BREAKOUT_RETEST"){
    const setup=setupAt(c15,i); if(!trend||!btc||!setup||rv<1.5||taker<0.56||momentum<52||momentum>66)return null;
    const stop=Math.min(setup.retest.l*0.998,entry-1.15*a); return {entry,stop,targetR:3.25};
  }
  if(name==="TREND_PULLBACK"){
    const e20=ema(closes,20),prev=c15[i-1],prevEma=ema(closes.slice(0,-1),20);
    if(!trend||!btc||!(prev.l<=prevEma*1.003&&prev.c<=prevEma*1.008&&bar.c>e20&&bar.c>bar.o)||rv<0.8||taker<0.52||momentum<46||momentum>62)return null;
    const stop=Math.min(...c15.slice(i-5,i+1).map(x=>x.l))*0.998; return {entry,stop,targetR:2.5};
  }
  if(name==="MOMENTUM_CONTINUATION"){
    const priorHigh=Math.max(...recent.map(x=>x.h));
    if(!trend||!btc||bar.c<=priorHigh*1.001||rv<1.2||taker<0.54||momentum<55||momentum>70)return null;
    return {entry,stop:entry-1.5*a,targetR:3};
  }
  if(name==="RANGE_MEAN_REVERSION"){
    const h1e20=ema(h1.map(x=>x.c),20),h1e50=ema(h1.map(x=>x.c),50),mean=ema(closes,20);
    const sd=Math.sqrt(recent.map(x=>(x.c-mean)**2).reduce((s,x)=>s+x,0)/recent.length), lower=mean-2*sd;
    const ranging=Math.abs(h1e20-h1e50)/h1.at(-1).c<0.006;
    if(!ranging||!btc||bar.c>lower||momentum>35||taker>0.49)return null;
    const stop=entry-1.5*a, risk=entry-stop, reward=mean-entry; if(reward/risk<1.5)return null;
    return {entry,stop,target:mean};
  }
  return null;
}

function simulate(strategy, symbol, c15, c1h, c4h, btc1h, btc4h) {
  const trades=[]; let equity=capital, open=null;
  for(let i=60;i<c15.length-1;i++){
    const bar=c15[i];
    if(open){
      const next=c15[i]; let exit=null,reason=null;
      // Conservative ambiguity rule: stop wins when both levels occur.
      if(next.l<=open.stop){exit=open.stop*(1-slippage);reason="STOP";}
      else if(next.h>=open.target){exit=open.target*(1-slippage);reason="TARGET";}
      else if(i-open.i>=96){exit=next.c*(1-slippage);reason="TIME";}
      if(exit){const gross=open.qty*(exit-open.entry);const costs=open.qty*(open.entry+exit)*fee;const pnl=gross-costs;equity+=pnl;trades.push({...open,exit,pnl,reason,exitTime:next.closeTime});open=null;}
      continue;
    }
    const h1=latestClosed(c1h,bar.closeTime), h4=latestClosed(c4h,bar.closeTime), bh1=latestClosed(btc1h,bar.closeTime), bh4=latestClosed(btc4h,bar.closeTime);
    if(h1.length<51||h4.length<51||bh1.length<51||bh4.length<51) continue;
    const signal=strategySignal(strategy,c15,i,h1,h4,bh1,bh4); if(!signal)continue;
    const {entry,stop}=signal, riskUnit=entry-stop;
    if(!(riskUnit>0&&riskUnit/entry<=0.03))continue;
    const target=signal.target || entry+signal.targetR*riskUnit, riskBudget=equity*riskFraction, qty=riskBudget/(riskUnit+entry*fee+stop*fee);
    if(!(qty>0))continue;
    open={symbol,i,entry,stop,target,qty,entryTime:bar.closeTime,equityBefore:equity};
  }
  return trades;
}

function metrics(trades) {
  const pnl=trades.map(x=>x.pnl), wins=pnl.filter(x=>x>0), losses=pnl.filter(x=>x<0);
  let curve=capital,peak=capital,maxDD=0,streak=0,longest=0;
  for(const p of pnl){curve+=p;peak=Math.max(peak,curve);maxDD=Math.max(maxDD,(peak-curve)/peak);if(p<0){streak++;longest=Math.max(longest,streak);}else streak=0;}
  const sum=a=>a.reduce((x,y)=>x+y,0), expectancy=pnl.length?sum(pnl)/pnl.length:0;
  return { trades:pnl.length, netReturnPct:+((curve/capital-1)*100).toFixed(3), winRatePct:+(pnl.length?wins.length/pnl.length*100:0).toFixed(2), expectancy:+expectancy.toFixed(5), profitFactor:+(losses.length?sum(wins)/Math.abs(sum(losses)):0).toFixed(3), maxDrawdownPct:+(maxDD*100).toFixed(3), averageWin:+(wins.length?sum(wins)/wins.length:0).toFixed(5), averageLoss:+(losses.length?sum(losses)/losses.length:0).toFixed(5), longestLosingStreak:longest };
}

const btc1h=await klines("BTCUSDT","1h"), btc4h=await klines("BTCUSDT","4h"), datasets=new Map();
for(const symbol of symbols)datasets.set(symbol,await Promise.all([klines(symbol,"15m"),klines(symbol,"1h"),klines(symbol,"4h")]));
const strategies=["BREAKOUT_RETEST","TREND_PULLBACK","MOMENTUM_CONTINUATION","RANGE_MEAN_REVERSION"];
const reports=[];
for(const strategy of strategies){let all=[];for(const symbol of symbols){const [m15,h1,h4]=datasets.get(symbol);all.push(...simulate(strategy,symbol,m15,h1,h4,btc1h,btc4h));}all.sort((a,b)=>a.entryTime-b.entryTime);const split=Math.floor(all.length*0.7),train=all.slice(0,split),test=all.slice(split),folds=[];for(let i=0;i<3;i++){const a=Math.floor(all.length*(0.4+i*0.2)),b=Math.floor(all.length*(0.6+i*0.2));if(b>a)folds.push(metrics(all.slice(a,b)));}const oos=metrics(test);reports.push({strategy,inSample:metrics(train),outOfSample:oos,walkForwardFolds:folds,all:metrics(all),pass:{minTrades200:all.length>=200,outOfSampleExpectancyPositive:oos.expectancy>0,outOfSampleProfitFactorAbove1_2:oos.profitFactor>1.2,allWalkForwardFoldsPositive:folds.length===3&&folds.every(x=>x.expectancy>0&&x.profitFactor>1.2)}});}
console.log(JSON.stringify({generatedAt:new Date().toISOString(),source:"Binance Spot closed OHLCV",symbols,days,assumptions:{feeEachSide:fee,slippageEachSide:slippage,sameBarStopAndTarget:"STOP_FIRST",historicalOrderBook:"UNAVAILABLE_NOT_SIMULATED",parameters:"PRE_REGISTERED_NO_POST_RESULT_TUNING"},reports},null,2));
