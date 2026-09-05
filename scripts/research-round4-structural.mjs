import fs from 'node:fs/promises';

const BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision'];
const SYMBOLS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT'];
const DAYS=1825,GATE={mean:0.015,hit:0.55,mfeMae:2.0};
async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path,{signal:AbortSignal.timeout(20000)});if(r.ok)return await r.json();last=`${b}:${r.status}`;}catch(e){last=e.message;}}throw new Error(last||path);}
async function klines(symbol){const out=[];let cur=Date.now()-DAYS*86400000,now=Date.now();while(cur<now){const p=await get(`/api/v3/klines?symbol=${symbol}&interval=1h&limit=1000&startTime=${cur}`);if(!p.length)break;for(const k of p)out.push({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],n:+k[8],tb:+k[9],tq:+k[10],ct:+k[6]});const nx=+p.at(-1)[6]+1;if(nx<=cur)break;cur=nx;if(p.length<1000)break;}return out.filter(x=>x.ct<now);}
const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:0;function median(a){if(!a.length)return NaN;const x=[...a].sort((a,b)=>a-b),m=Math.floor(x.length/2);return x.length%2?x[m]:(x[m-1]+x[m])/2;}const ret=(r,i,n)=>i>=n?r[i].c/r[i-n].c-1:NaN;
function stats(events,rows,horizons){const out={};for(const h of horizons){const vals=[],mfes=[],maes=[];for(const e of events){const r=rows.get(e.sym),entryI=e.i+1,exitI=e.i+h;if(!r||exitI>=r.length)continue;const entry=r[entryI].o,exit=r[exitI].c;if(!(entry>0&&exit>0))continue;const p=r.slice(entryI,exitI+1);vals.push(exit/entry-1);mfes.push(Math.max(...p.map(x=>x.h))/entry-1);maes.push(Math.abs(Math.min(0,Math.min(...p.map(x=>x.l))/entry-1)));}const s={n:vals.length,meanGross:mean(vals),medianGross:median(vals),hitRate:vals.length?vals.filter(x=>x>0).length/vals.length:0,medianMFE:median(mfes),medianMAE:median(maes)};s.medianMfeMaeRatio=s.medianMAE===0?(s.medianMFE>0?Infinity:0):s.medianMFE/s.medianMAE;s.pass=s.meanGross>=GATE.mean&&s.medianGross>0&&s.hitRate>GATE.hit&&s.medianMfeMaeRatio>=GATE.mfeMae;out[h]=s;}return out;}
const rows=new Map();for(const s of SYMBOLS){try{const r=await klines(s);if(r.length>=24*180){rows.set(s,r);console.error('downloaded',s,r.length)}}catch(e){console.error('skip',s,e.message)}}
const byTime=new Map();for(const [sym,r] of rows){for(let i=24;i<r.length;i++){const t=r[i].t;if(!byTime.has(t))byTime.set(t,[]);byTime.get(t).push({sym,r24:ret(r,i,24)});}}
const market=new Map();const breadth=new Map();for(const [t,a] of byTime){const rs=a.map(x=>x.r24).filter(Number.isFinite);market.set(t,median(rs));breadth.set(t,rs.length?rs.filter(x=>x>0).length/rs.length:NaN);}
const result={generatedAt:new Date().toISOString(),status:'RESEARCH_ONLY',protocol:'ROUND4_STRUCTURAL_RAW_EVENT_V1',days:DAYS,gate:GATE,hypotheses:{}};

// H1: residual dislocation + market-neutral exhaustion confirmation.
{
 const events=[];for(const [sym,r] of rows){const residuals=[];for(let i=24;i<r.length;i++){const a=ret(r,i,24),m=market.get(r[i].t);if(!Number.isFinite(a)||!Number.isFinite(m))continue;const res=a-m;residuals.push(res);if(residuals.length<720)continue;const w=residuals.slice(-720),mu=mean(w),sd=Math.sqrt(mean(w.map(x=>(x-mu)**2)));if(!(sd>0))continue;const z=(res-mu)/sd;const prev=i>24?ret(r,i-1,24)-market.get(r[i-1].t):NaN;const volWin=r.slice(Math.max(0,i-168),i).map(x=>Math.log1p(x.v)),vm=mean(volWin),vs=Math.sqrt(mean(volWin.map(x=>(x-vm)**2))),vz=vs>0?(Math.log1p(r[i].v)-vm)/vs:NaN;const one=ret(r,i,1),taker=r[i].q>0?r[i].tq/r[i].q:NaN;if(z<=-3.25&&one>0&&vz>=1.5&&taker>=0.55&&Number.isFinite(prev)&&res>prev)events.push({sym,i,t:r[i].t});}}
 const horizons=stats(events,rows,[24,48,72]);result.hypotheses.residualNeutralExhaustion={definition:{residualZ:-3.25,volumeZ:1.5,takerBuyQuoteShare:0.55,reversal:'current residual > previous residual and 1h return > 0'},eventCount:events.length,horizons,rawGatePassed:Object.values(horizons).some(x=>x.pass)};
}

// H2: breadth inflection after broad selloff, not continuation.
{
 const events=[];for(const [sym,r] of rows){for(let i=30;i<r.length;i++){const b0=breadth.get(r[i].t),b6=breadth.get(r[i-6].t),m24=market.get(r[i].t),m6=market.get(r[i-6].t);if(![b0,b6,m24,m6].every(Number.isFinite))continue;const asset24=ret(r,i,24);if(b6<=0.30&&b0>=0.50&&m24<0&&m24>m6&&asset24<0&&ret(r,i,1)>0)events.push({sym,i,t:r[i].t});}}
 const horizons=stats(events,rows,[24,48,72]);result.hypotheses.breadthInflection={definition:{breadth6hAgoMax:0.30,breadthNowMin:0.50,market24:'negative but improving vs 6h ago',asset24:'negative',confirmation:'1h return > 0'},eventCount:events.length,horizons,rawGatePassed:Object.values(horizons).some(x=>x.pass)};
}

// H3: forced-liquidation proxy using extreme downside, volume, trade-count and taker imbalance reversal.
{
 const events=[];for(const [sym,r] of rows){for(let i=168;i<r.length;i++){const rr=ret(r,i,6);if(!Number.isFinite(rr))continue;const vw=r.slice(i-168,i).map(x=>Math.log1p(x.v)),nw=r.slice(i-168,i).map(x=>Math.log1p(x.n)),vm=mean(vw),vs=Math.sqrt(mean(vw.map(x=>(x-vm)**2))),nm=mean(nw),ns=Math.sqrt(mean(nw.map(x=>(x-nm)**2)));const vz=vs>0?(Math.log1p(r[i].v)-vm)/vs:NaN,nz=ns>0?(Math.log1p(r[i].n)-nm)/ns:NaN;const taker=r[i].q>0?r[i].tq/r[i].q:NaN;const one=ret(r,i,1);if(rr<=-0.06&&vz>=2.5&&nz>=2.0&&taker>=0.58&&one>0)events.push({sym,i,t:r[i].t});}}
 const horizons=stats(events,rows,[24,48,72]);result.hypotheses.forcedLiquidationProxy={definition:{ret6hMax:-0.06,volumeZ:2.5,tradeCountZ:2.0,takerBuyQuoteShare:0.58,confirmation:'1h return > 0'},eventCount:events.length,horizons,rawGatePassed:Object.values(horizons).some(x=>x.pass)};
}

result.anyRawGatePassed=Object.values(result.hypotheses).some(x=>x.rawGatePassed);result.verdict=result.anyRawGatePassed?'FREEZE_PASSING_DEFINITIONS_AND_PROCEED_OOS':'REJECT_ROUND4_AND_OPEN_NEW_HYPOTHESES';
await fs.mkdir('validation',{recursive:true});await fs.writeFile('validation/round4-structural-discovery.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));