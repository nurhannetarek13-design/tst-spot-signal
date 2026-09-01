import fs from 'node:fs/promises';

const BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision'];
const DISCOVERY=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT'];
const HOLDOUT=new Set(['XLMUSDT','HBARUSDT','ICPUSDT','ALGOUSDT','VETUSDT','THETAUSDT','RUNEUSDT','GRTUSDT']);
const DAYS=1825;
const H=[24,48,72];
const GATE={mean:0.015,hit:0.55,mfeMae:2.0};
const TH={marketZ:-2.5,breadth:0.60,assetRet:-0.03,volumeZ:2.5};

async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path,{signal:AbortSignal.timeout(20000)});if(r.ok)return await r.json();last=`${b}:${r.status}`;}catch(e){last=e.message;}}throw new Error(last||path);}
async function klines(symbol){const out=[];let cur=Date.now()-DAYS*86400000;const now=Date.now();while(cur<now){const p=await get(`/api/v3/klines?symbol=${symbol}&interval=1h&limit=1000&startTime=${cur}`);if(!p.length)break;for(const k of p)out.push({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],ct:+k[6]});const n=+p.at(-1)[6]+1;if(n<=cur)break;cur=n;if(p.length<1000)break;}return out.filter(x=>x.ct<now);}
const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:0;
function median(a){if(!a.length)return NaN;const x=[...a].sort((a,b)=>a-b),m=Math.floor(x.length/2);return x.length%2?x[m]:(x[m-1]+x[m])/2;}
function sd(a){if(a.length<2)return 0;const m=mean(a);return Math.sqrt(a.reduce((s,x)=>s+(x-m)**2,0)/(a.length-1));}
function ret(r,i,n){return i>=n?r[i].c/r[i-n].c-1:NaN;}
function z(hist,x){const s=sd(hist);return s?(x-mean(hist))/s:NaN;}

const rows=new Map();for(const s of DISCOVERY){if(HOLDOUT.has(s))continue;try{const r=await klines(s);if(r.length>=24*180){rows.set(s,r);console.error('downloaded',s,r.length)}}catch(e){console.error('skip',s,e.message)}}
const btc=rows.get('BTCUSDT');if(!btc)throw new Error('BTCUSDT unavailable');
const byTime=new Map();for(const [sym,r] of rows){for(let i=1;i<r.length;i++){const t=r[i].t;if(!byTime.has(t))byTime.set(t,[]);byTime.get(t).push({sym,r:ret(r,i,1)});}}
const market=[];for(const [t,a] of [...byTime].sort((a,b)=>a[0]-b[0])){const vals=a.map(x=>x.r).filter(Number.isFinite);market.push({t,r:median(vals),breadth:vals.length?vals.filter(x=>x<=TH.assetRet).length/vals.length:0});}
const mi=new Map(market.map((x,i)=>[x.t,i]));
const events=[];
for(const [sym,r] of rows){for(let i=24*30;i<r.length-73;i++){const mIdx=mi.get(r[i].t);if(mIdx==null||mIdx<24*30)continue;const mh=market.slice(mIdx-24*30,mIdx).map(x=>x.r).filter(Number.isFinite);const mz=z(mh,market[mIdx].r);const r1=ret(r,i,1),prev=ret(r,i-1,1);const lv=Math.log1p(r[i].v),vh=r.slice(i-24*7,i).map(x=>Math.log1p(x.v));const vz=z(vh,lv);if(!(mz<=TH.marketZ&&market[mIdx].breadth>=TH.breadth&&r1<=TH.assetRet&&vz>=TH.volumeZ&&r1>prev))continue;events.push({sym,t:r[i].t,i});}}

const result={generatedAt:new Date().toISOString(),status:'RESEARCH_ONLY',protocol:'validation/evidence-gate-round3.json',category:'LIQUIDITY_CRASH_EXHAUSTION',discoverySymbols:[...rows.keys()],days:DAYS,thresholds:TH,eventCount:events.length,horizons:{}};
for(const h of H){const vals=[],mfes=[],maes=[];for(const e of events){const r=rows.get(e.sym),entryI=e.i+1,exitI=e.i+h;if(exitI>=r.length)continue;const entry=r[entryI].o,exit=r[exitI].c;if(!(entry>0&&exit>0))continue;const path=r.slice(entryI,exitI+1);vals.push(exit/entry-1);mfes.push(Math.max(...path.map(x=>x.h))/entry-1);maes.push(Math.abs(Math.min(0,Math.min(...path.map(x=>x.l))/entry-1)));}const s={n:vals.length,meanGross:mean(vals),medianGross:median(vals),hitRate:vals.length?vals.filter(x=>x>0).length/vals.length:0,medianMFE:median(mfes),medianMAE:median(maes)};s.medianMfeMaeRatio=s.medianMAE===0?(s.medianMFE>0?Infinity:0):s.medianMFE/s.medianMAE;s.pass=s.meanGross>=GATE.mean&&s.medianGross>0&&s.hitRate>GATE.hit&&s.medianMfeMaeRatio>=GATE.mfeMae;result.horizons[h]=s;}
result.rawGatePassed=Object.values(result.horizons).some(x=>x.pass);result.verdict=result.rawGatePassed?'FREEZE_EVENT_AND_PROCEED_OOS':'REJECT_BEFORE_STRATEGY_CONSTRUCTION';
await fs.mkdir('validation',{recursive:true});await fs.writeFile('validation/round3-crash-discovery.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));