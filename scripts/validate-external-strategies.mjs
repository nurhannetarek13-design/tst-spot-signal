import fs from 'node:fs/promises';

const API = 'https://api.binance.com';
const SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT'];
const FEE = 0.001;
const SLIPPAGE = 0.0005;
const ROUND_TRIP_DRAG = 2 * (FEE + SLIPPAGE);

async function fetchKlines(symbol, interval, target) {
  const out = [];
  let endTime;
  while (out.length < target) {
    const limit = Math.min(1000, target - out.length);
    const q = new URLSearchParams({ symbol, interval, limit: String(limit) });
    if (endTime) q.set('endTime', String(endTime));
    const r = await fetch(`${API}/api/v3/klines?${q}`);
    if (!r.ok) throw new Error(`BINANCE_${r.status}_${symbol}_${interval}`);
    const rows = await r.json();
    if (!Array.isArray(rows) || rows.length === 0) break;
    out.unshift(...rows);
    endTime = Number(rows[0][0]) - 1;
    if (rows.length < limit) break;
    await new Promise(r => setTimeout(r, 120));
  }
  return out.slice(-target).map(k => ({
    t: Number(k[0]), open: Number(k[1]), high: Number(k[2]), low: Number(k[3]), close: Number(k[4]), volume: Number(k[5]), closeTime: Number(k[6])
  }));
}

function sma(v,p){const o=Array(v.length).fill(null);let s=0;for(let i=0;i<v.length;i++){s+=v[i];if(i>=p)s-=v[i-p];if(i>=p-1)o[i]=s/p;}return o;}
function ema(v,p){const o=Array(v.length).fill(null);if(v.length<p)return o;let s=0;for(let i=0;i<p;i++)s+=v[i];o[p-1]=s/p;const k=2/(p+1);for(let i=p;i<v.length;i++)o[i]=v[i]*k+o[i-1]*(1-k);return o;}
function roc(v,p){const o=Array(v.length).fill(null);for(let i=p;i<v.length;i++)o[i]=v[i-p]?v[i]/v[i-p]-1:null;return o;}
function crossAbove(a,b,i){return i>0&&a[i-1]!=null&&b[i-1]!=null&&a[i]!=null&&b[i]!=null&&a[i-1]<=b[i-1]&&a[i]>b[i];}
function crossBelow(a,b,i){return i>0&&a[i-1]!=null&&b[i-1]!=null&&a[i]!=null&&b[i]!=null&&a[i-1]>=b[i-1]&&a[i]<b[i];}

function metrics(trades){
  const rets=trades.map(t=>t.netReturn);
  const wins=rets.filter(x=>x>0), losses=rets.filter(x=>x<0);
  const grossProfit=wins.reduce((a,b)=>a+b,0), grossLoss=-losses.reduce((a,b)=>a+b,0);
  let equity=1,peak=1,maxDD=0;
  for(const r of rets){equity*=1+r;peak=Math.max(peak,equity);maxDD=Math.max(maxDD,(peak-equity)/peak);}
  return {
    trades: trades.length,
    profitFactor: grossLoss>0?grossProfit/grossLoss:(grossProfit>0?999:0),
    expectancy: rets.length?rets.reduce((a,b)=>a+b,0)/rets.length:0,
    winRate: rets.length?wins.length/rets.length:0,
    maxDrawdown: maxDD,
    totalReturn: equity-1,
  };
}

function runEmaPhase(hourly){
  const c=hourly.map(x=>x.close), ef=ema(c,10), es=ema(c,20), s50=sma(c,50), s200=sma(c,200);
  const trades=[]; let pos=null;
  for(let i=201;i<hourly.length-1;i++){
    const bull=c[i]>s50[i]&&c[i]>s200[i];
    if(!pos && bull && crossAbove(ef,es,i)){
      const entry=hourly[i+1].open*(1+SLIPPAGE); pos={entry,entryTime:hourly[i+1].t}; continue;
    }
    if(pos && crossBelow(ef,es,i)){
      const exit=hourly[i+1].open*(1-SLIPPAGE); const gross=exit/pos.entry-1; trades.push({...pos,exit,exitTime:hourly[i+1].t,grossReturn:gross,netReturn:gross-2*FEE}); pos=null;
    }
  }
  if(pos){const exit=hourly.at(-1).close*(1-SLIPPAGE);const gross=exit/pos.entry-1;trades.push({...pos,exit,exitTime:hourly.at(-1).t,grossReturn:gross,netReturn:gross-2*FEE,forcedClose:true});}
  return trades;
}

function dailyIndexAt(daily, ts){let lo=0,hi=daily.length-1,ans=-1;while(lo<=hi){const m=(lo+hi)>>1;if(daily[m].closeTime<ts){ans=m;lo=m+1;}else hi=m-1;}return ans;}

function runDualMomentum(hourly,daily){
  const hc=hourly.map(x=>x.close), hs50=sma(hc,50),hs200=sma(hc,200),hr=roc(hc,60);
  const dc=daily.map(x=>x.close), ds50=sma(dc,50),ds200=sma(dc,200),dr=roc(dc,60);
  const trades=[]; let pos=null;
  for(let i=201;i<hourly.length-1;i++){
    const di=dailyIndexAt(daily,hourly[i].t); if(di<200) continue;
    const currentBull=hc[i]>hs50[i]&&hs50[i]>hs200[i]&&hr[i]>0;
    const dailyBull=dc[di]>ds50[di]&&ds50[di]>ds200[di]&&dr[di]>0;
    const signal=currentBull&&dailyBull;
    if(!pos && signal){const entry=hourly[i+1].open*(1+SLIPPAGE);pos={entry,entryTime:hourly[i+1].t};continue;}
    if(pos && !signal){const exit=hourly[i+1].open*(1-SLIPPAGE);const gross=exit/pos.entry-1;trades.push({...pos,exit,exitTime:hourly[i+1].t,grossReturn:gross,netReturn:gross-2*FEE});pos=null;}
  }
  if(pos){const exit=hourly.at(-1).close*(1-SLIPPAGE);const gross=exit/pos.entry-1;trades.push({...pos,exit,exitTime:hourly.at(-1).t,grossReturn:gross,netReturn:gross-2*FEE,forcedClose:true});}
  return trades;
}

const gate={minTrades:20,minPF:1.25,minExpectancy:0.001,minWinRate:0.45,maxDD:0.15};
function decide(m){const reasons=[];if(m.trades<gate.minTrades)reasons.push('TOO_FEW_TRADES');if(m.profitFactor<gate.minPF)reasons.push('PF_FAIL');if(m.expectancy<gate.minExpectancy)reasons.push('EXPECTANCY_FAIL');if(m.winRate<gate.minWinRate)reasons.push('WIN_RATE_FAIL');if(m.maxDrawdown>gate.maxDD)reasons.push('DRAWDOWN_FAIL');return {validated:reasons.length===0,reasons};}

const report={generatedAt:new Date().toISOString(),source:'Binance public klines',assumptions:{feePerSide:FEE,slippagePerSide:SLIPPAGE,execution:'next-bar-open',closedCandlesOnly:true,roundTripDrag:ROUND_TRIP_DRAG},gate,symbols:{}};
for(const symbol of SYMBOLS){
  const [hourly,daily]=await Promise.all([fetchKlines(symbol,'1h',4000),fetchKlines(symbol,'1d',500)]);
  const emaTrades=runEmaPhase(hourly), dualTrades=runDualMomentum(hourly,daily);
  const emaM=metrics(emaTrades), dualM=metrics(dualTrades);
  report.symbols[symbol]={bars:{hourly:hourly.length,daily:daily.length},strategies:{
    bec_ema_cross_with_market_phases:{metrics:emaM,decision:decide(emaM)},
    bec_dual_momentum_simple:{metrics:dualM,decision:decide(dualM)}
  }};
}
for(const id of ['bec_ema_cross_with_market_phases','bec_dual_momentum_simple']){
  const all=Object.values(report.symbols).flatMap(s=>s.strategies[id].metrics?[]:[]);
  const perSymbol=Object.entries(report.symbols).map(([symbol,s])=>({symbol,...s.strategies[id]}));
  report[id]={perSymbol,validatedAcrossAll:perSymbol.every(x=>x.decision.validated)};
}
await fs.mkdir('validation/external-strategies',{recursive:true});
await fs.writeFile('validation/external-strategies/latest.json',JSON.stringify(report,null,2));
console.log(JSON.stringify(report,null,2));
