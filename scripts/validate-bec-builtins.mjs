import fs from 'node:fs/promises';

const BASES=['https://data-api.binance.vision','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://api.binance.com'];
const SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT'];
const FEE=0.001, SLIP=0.0005;
const GATE={minTrades:20,minPF:1.25,minExpectancy:0.001,minWinRate:0.45,maxDD:0.15};

async function get(path){const fails=[];for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return {data:await r.json(),base:b};fails.push(`${b}:${r.status}`);}catch(e){fails.push(`${b}:${e?.name||'ERR'}`);}}throw new Error('BINANCE_PUBLIC_UNAVAILABLE '+fails.join(','));}
async function klines(symbol,interval,target){let out=[],endTime,sourceBase;while(out.length<target){const limit=Math.min(1000,target-out.length);const q=new URLSearchParams({symbol,interval,limit:String(limit)});if(endTime)q.set('endTime',String(endTime));const p=await get(`/api/v3/klines?${q}`);sourceBase??=p.base;const rows=p.data;if(!Array.isArray(rows)||!rows.length)break;out.unshift(...rows);endTime=Number(rows[0][0])-1;if(rows.length<limit)break;await new Promise(r=>setTimeout(r,100));}return {sourceBase,rows:out.slice(-target).map(k=>({t:+k[0],open:+k[1],high:+k[2],low:+k[3],close:+k[4],closeTime:+k[6]}))};}

function sma(v,p){const o=Array(v.length).fill(null);let s=0;for(let i=0;i<v.length;i++){s+=v[i];if(i>=p)s-=v[i-p];if(i>=p-1)o[i]=s/p;}return o;}
function ema(v,p){const o=Array(v.length).fill(null);if(v.length<p)return o;let s=0;for(let i=0;i<p;i++)s+=v[i];o[p-1]=s/p;const k=2/(p+1);for(let i=p;i<v.length;i++)o[i]=v[i]*k+o[i-1]*(1-k);return o;}
function wma(v,p){const o=Array(v.length).fill(null),den=p*(p+1)/2;for(let i=p-1;i<v.length;i++){let s=0;for(let j=0;j<p;j++)s+=v[i-p+1+j]*(j+1);o[i]=s/den;}return o;}
function hma(v,p){const a=wma(v,Math.floor(p/2)),b=wma(v,p),root=Math.max(1,Math.floor(Math.sqrt(p))),d=v.map((_,i)=>a[i]!=null&&b[i]!=null?2*a[i]-b[i]:null),o=Array(v.length).fill(null),den=root*(root+1)/2;for(let i=root-1;i<v.length;i++){let s=0,ok=true;for(let j=0;j<root;j++){const x=d[i-root+1+j];if(x==null){ok=false;break;}s+=x*(j+1);}if(ok)o[i]=s/den;}return o;}
function rsi(v,p=14){const o=Array(v.length).fill(null);if(v.length<=p)return o;let g=0,l=0;for(let i=1;i<=p;i++){const d=v[i]-v[i-1];if(d>=0)g+=d;else l-=d;}g/=p;l/=p;o[p]=l===0?100:100-100/(1+g/l);for(let i=p+1;i<v.length;i++){const d=v[i]-v[i-1];g=(g*(p-1)+Math.max(d,0))/p;l=(l*(p-1)+Math.max(-d,0))/p;o[i]=l===0?100:100-100/(1+g/l);}return o;}
function linreg(v,p){const o=Array(v.length).fill(null),sx=(p-1)*p/2,sxx=(p-1)*p*(2*p-1)/6,den=p*sxx-sx*sx;for(let i=p-1;i<v.length;i++){let sy=0,sxy=0;for(let j=0;j<p;j++){const y=v[i-p+1+j];sy+=y;sxy+=j*y;}const m=(p*sxy-sx*sy)/den,b=(sy-m*sx)/p;o[i]=b+m*(p-1);}return o;}
function roc(v,p){const o=Array(v.length).fill(null);for(let i=p;i<v.length;i++)o[i]=v[i-p]?v[i]/v[i-p]-1:null;return o;}
function ca(a,b,i){return i>0&&a[i-1]!=null&&b[i-1]!=null&&a[i]!=null&&b[i]!=null&&a[i-1]<=b[i-1]&&a[i]>b[i];}
function cb(a,b,i){return i>0&&a[i-1]!=null&&b[i-1]!=null&&a[i]!=null&&b[i]!=null&&a[i-1]>=b[i-1]&&a[i]<b[i];}

function simulate(bars,buy,sell,start=1){const trades=[];let pos=null;for(let i=start;i<bars.length-1;i++){if(!pos&&buy(i)){const entry=bars[i+1].open*(1+SLIP);pos={entry,entryTime:bars[i+1].t};continue;}if(pos&&sell(i)){const exit=bars[i+1].open*(1-SLIP),gross=exit/pos.entry-1;trades.push({...pos,exit,exitTime:bars[i+1].t,netReturn:gross-2*FEE});pos=null;}}if(pos){const exit=bars.at(-1).close*(1-SLIP),gross=exit/pos.entry-1;trades.push({...pos,exit,exitTime:bars.at(-1).t,netReturn:gross-2*FEE,forcedClose:true});}return trades;}
function metrics(trades){const r=trades.map(x=>x.netReturn),w=r.filter(x=>x>0),l=r.filter(x=>x<0),gp=w.reduce((a,b)=>a+b,0),gl=-l.reduce((a,b)=>a+b,0);let eq=1,pk=1,dd=0;for(const x of r){eq*=1+x;pk=Math.max(pk,eq);dd=Math.max(dd,(pk-eq)/pk);}return {trades:r.length,profitFactor:gl>0?gp/gl:(gp>0?999:0),expectancy:r.length?r.reduce((a,b)=>a+b,0)/r.length:0,winRate:r.length?w.length/r.length:0,maxDrawdown:dd,totalReturn:eq-1};}
function decide(m){const reasons=[];if(m.trades<GATE.minTrades)reasons.push('TOO_FEW_TRADES');if(m.profitFactor<GATE.minPF)reasons.push('PF_FAIL');if(m.expectancy<GATE.minExpectancy)reasons.push('EXPECTANCY_FAIL');if(m.winRate<GATE.minWinRate)reasons.push('WIN_RATE_FAIL');if(m.maxDrawdown>GATE.maxDD)reasons.push('DRAWDOWN_FAIL');return {validated:reasons.length===0,reasons};}
function pool(list){return metrics(list.flat());}
function dailyIndexAt(daily,ts){let lo=0,hi=daily.length-1,ans=-1;while(lo<=hi){const m=(lo+hi)>>1;if(daily[m].closeTime<ts){ans=m;lo=m+1;}else hi=m-1;}return ans;}

function runHourly(hourly,daily){const c=hourly.map(x=>x.close),e10=ema(c,10),e20=ema(c,20),s50=sma(c,50),s200=sma(c,200),hf=hma(c,20),hs=hma(c,70),rr=rsi(c,14),lr=linreg(c,50),hr=roc(c,60);const dc=daily.map(x=>x.close),ds50=sma(dc,50),ds200=sma(dc,200),dr=roc(dc,60);
  const emaCross=simulate(hourly,i=>ca(e10,e20,i),i=>cb(e10,e20,i),25);
  const emaPhase=simulate(hourly,i=>c[i]>s50[i]&&c[i]>s200[i]&&ca(e10,e20,i),i=>cb(e10,e20,i),201);
  const marketPhases=simulate(hourly,i=>c[i]>s50[i]&&c[i]>s200[i],i=>c[i]<s50[i]||c[i]<s200[i],201);
  const hmaRsi=simulate(hourly,i=>ca(hf,hs,i)&&rr[i]>52&&c[i]>lr[i],i=>cb(hf,hs,i),205);
  const dual=simulate(hourly,i=>{const di=dailyIndexAt(daily,hourly[i].t);return di>=200&&c[i]>s50[i]&&s50[i]>s200[i]&&hr[i]>0&&dc[di]>ds50[di]&&ds50[di]>ds200[di]&&dr[di]>0;},i=>{const di=dailyIndexAt(daily,hourly[i].t);return di<200||!(c[i]>s50[i]&&s50[i]>s200[i]&&hr[i]>0&&dc[di]>ds50[di]&&ds50[di]>ds200[di]&&dr[di]>0);},201);
  return {bec_ema_cross:emaCross,bec_ema_cross_with_market_phases:emaPhase,bec_market_phases:marketPhases,bec_hma_rsi_linreg:hmaRsi,bec_dual_momentum_simple:dual};}
function runWeekly(weekly){const c=weekly.map(x=>x.close),e21=ema(c,21),s20=sma(c,20),e20=ema(c,20);return {bec_bullmarketsupportband:simulate(weekly,i=>ca(e21,s20,i),i=>cb(e21,s20,i),24),bec_wema20:simulate(weekly,i=>c[i]>e20[i],i=>c[i]<e20[i],21)};}

const report={generatedAt:new Date().toISOString(),source:'Binance public klines with endpoint failover',assumptions:{feePerSide:FEE,slippagePerSide:SLIP,execution:'next-bar-open',closedCandlesOnly:true},gate:GATE,symbols:{},strategies:{}};
const tradePool={};
for(const symbol of SYMBOLS){const [h,d,w]=await Promise.all([klines(symbol,'1h',4000),klines(symbol,'1d',500),klines(symbol,'1w',300)]);const all={...runHourly(h.rows,d.rows),...runWeekly(w.rows)};report.symbols[symbol]={sources:{hourly:h.sourceBase,daily:d.sourceBase,weekly:w.sourceBase},bars:{hourly:h.rows.length,daily:d.rows.length,weekly:w.rows.length},strategies:{}};for(const [id,trades] of Object.entries(all)){tradePool[id]??=[];tradePool[id].push(trades);const m=metrics(trades);report.symbols[symbol].strategies[id]={metrics:m,decision:decide(m)};}}
for(const id of Object.keys(tradePool)){const perSymbol=SYMBOLS.map(symbol=>({symbol,...report.symbols[symbol].strategies[id]})),pooled=pool(tradePool[id]);report.strategies[id]={perSymbol,pooled:{metrics:pooled,decision:decide(pooled)},symbolPassCount:perSymbol.filter(x=>x.decision.validated).length,validatedAcrossAll:perSymbol.every(x=>x.decision.validated)&&decide(pooled).validated,promotionEligible:perSymbol.every(x=>x.decision.validated)&&decide(pooled).validated};}
await fs.mkdir('validation/external-strategies',{recursive:true});await fs.writeFile('validation/external-strategies/all-bec-latest.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
