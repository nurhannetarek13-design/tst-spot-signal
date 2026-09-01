import fs from 'node:fs/promises';

const BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision'];
const SYMBOLS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT'];
const DAYS=1825, OOS_FRAC=.30, BASE_COST=.003, STRESS_COST=.006;
const GATE={minOosTrades:100,minPF:1.15,minPositiveFolds:4,maxDD:.20,minNeighborPositive:.70};

async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path,{signal:AbortSignal.timeout(25000)});if(r.ok)return r.json();last=`${b}:${r.status}`;}catch(e){last=e.message}}throw new Error(last||path)}
async function klines(symbol){const out=[];let cur=Date.now()-DAYS*86400000,now=Date.now();while(cur<now){const p=await get(`/api/v3/klines?symbol=${symbol}&interval=4h&limit=1000&startTime=${cur}`);if(!p.length)break;for(const k of p)out.push({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],ct:+k[6]});const n=+p.at(-1)[6]+1;if(n<=cur)break;cur=n;if(p.length<1000)break}return out.filter(x=>x.ct<now)}
const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:0;
function ema(vals,p){const out=new Array(vals.length).fill(NaN);if(vals.length<p)return out;let m=mean(vals.slice(0,p));out[p-1]=m;const k=2/(p+1);for(let i=p;i<vals.length;i++){m=vals[i]*k+m*(1-k);out[i]=m}return out}
function atr(r,p=14){const tr=r.map((x,i)=>i?Math.max(x.h-x.l,Math.abs(x.h-r[i-1].c),Math.abs(x.l-r[i-1].c)):x.h-x.l);return ema(tr,p)}
function rsi(r,p=14){const out=new Array(r.length).fill(NaN);let g=0,l=0;for(let i=1;i<=p&&i<r.length;i++){const d=r[i].c-r[i-1].c;if(d>=0)g+=d;else l-=d}if(r.length<=p)return out;g/=p;l/=p;out[p]=l===0?100:100-100/(1+g/l);for(let i=p+1;i<r.length;i++){const d=r[i].c-r[i-1].c;g=(g*(p-1)+Math.max(d,0))/p;l=(l*(p-1)+Math.max(-d,0))/p;out[i]=l===0?100:100-100/(1+g/l)}return out}
function q(arr,p){const a=[...arr].filter(Number.isFinite).sort((x,y)=>x-y);if(!a.length)return NaN;return a[Math.min(a.length-1,Math.max(0,Math.floor((a.length-1)*p)))]}
function maxDD(rets){let eq=1,peak=1,dd=0;for(const x of rets){eq*=1+x;peak=Math.max(peak,eq);dd=Math.max(dd,1-eq/peak)}return dd}
function metrics(trades,cost){const r=trades.map(x=>x.gross-cost),wins=r.filter(x=>x>0),loss=r.filter(x=>x<0);const gp=wins.reduce((s,x)=>s+x,0),gl=Math.abs(loss.reduce((s,x)=>s+x,0));return {n:r.length,expectancy:mean(r),pf:gl?gp/gl:(gp>0?999:0),winRate:r.length?wins.length/r.length:0,maxDD:maxDD(r),top2Share:gp?Math.max(0,...wins.sort((a,b)=>b-a).slice(0,2).reduce?[]:[]):0}}
function enrichMetrics(trades,cost){const m=metrics(trades,cost),profits=trades.map(x=>x.gross-cost).filter(x=>x>0).sort((a,b)=>b-a),gp=profits.reduce((s,x)=>s+x,0);m.top2Share=gp?profits.slice(0,2).reduce((s,x)=>s+x,0)/gp:0;return m}
function foldStats(trades,cost,start,end){const width=(end-start)/5;const folds=[];for(let f=0;f<5;f++){const a=start+f*width,b=f===4?end+1:start+(f+1)*width;const t=trades.filter(x=>x.t>=a&&x.t<b);folds.push({n:t.length,expectancy:mean(t.map(x=>x.gross-cost))})}return folds}

function prep(r){const c=r.map(x=>x.c),e50=ema(c,50),e200=ema(c,200),a14=atr(r,14),rs=rsi(r,14),atrPct=a14.map((x,i)=>x/r[i].c);return {e50,e200,a14,rs,atrPct}}
function signal(name,r,p,i,cfg){if(i<220)return false;const {e50,e200,a14,rs,atrPct}=p;if(!Number.isFinite(e200[i])||!Number.isFinite(a14[i]))return false;
 if(name==='MTF_TREND_BREAKOUT'){const hi=Math.max(...r.slice(i-cfg.breakoutBars,i).map(x=>x.h));return r[i].c>hi&&r[i].c>e200[i]&&e50[i]>e200[i]&&e50[i]>e50[i-6]}
 if(name==='TREND_PULLBACK_RECLAIM'){const recent=r.slice(i-3,i+1),touch=recent.some((x,k)=>{const j=i-3+k;return Number.isFinite(e50[j])&&x.l<=e50[j]+cfg.pullbackAtr*a14[j]});const hi=Math.max(...r.slice(i-cfg.reclaimBars,i).map(x=>x.h));return r[i].c>e200[i]&&e50[i]>e200[i]&&touch&&r[i].c>hi&&rs[i]>=45&&rs[i]<=65}
 if(name==='VOLATILITY_EXPANSION_TREND'){const hist=atrPct.slice(Math.max(0,i-cfg.compressionLookback),i),thr=q(hist,cfg.compressionQuantile),compressed=atrPct.slice(Math.max(0,i-6),i).some(x=>Number.isFinite(x)&&x<=thr),hi=Math.max(...r.slice(i-cfg.breakoutBars,i).map(x=>x.h));return r[i].c>e200[i]&&compressed&&r[i].c>hi}
 return false}
function simulate(name,r,cfg){const p=prep(r),trades=[];let pos=null;for(let i=220;i<r.length-1;i++){
 if(pos){const bar=r[i],atrNow=p.a14[i];if(bar.l<=pos.stop){trades.push({t:bar.t,gross:pos.stop/pos.entry-1});pos=null;continue}pos.peak=Math.max(pos.peak,bar.h);const trail=pos.peak-cfg.trailAtr*atrNow;pos.stop=Math.max(pos.stop,trail);pos.held++;
 const trendExit=name==='VOLATILITY_EXPANSION_TREND'?bar.c<p.e200[i]:bar.c<p.e50[i];if(trendExit||pos.held>=cfg.maxHoldBars){trades.push({t:bar.t,gross:bar.c/pos.entry-1});pos=null;continue}}
 if(!pos&&signal(name,r,p,i,cfg)){const entry=r[i+1].o,aa=p.a14[i];if(entry>0&&aa>0)pos={entry,stop:entry-cfg.initialStopAtr*aa,peak:entry,held:0}}
 }
 return trades}

const BASECFG={
 MTF_TREND_BREAKOUT:{breakoutBars:30,initialStopAtr:2.5,trailAtr:3.0,maxHoldBars:90},
 TREND_PULLBACK_RECLAIM:{pullbackAtr:.60,reclaimBars:3,initialStopAtr:2.25,trailAtr:3.0,maxHoldBars:72},
 VOLATILITY_EXPANSION_TREND:{compressionLookback:120,compressionQuantile:.25,breakoutBars:20,initialStopAtr:2.5,trailAtr:3.25,maxHoldBars:90}
};
const NEIGHBORS={
 MTF_TREND_BREAKOUT:[24,30,36].flatMap(b=>[2.25,2.5,2.75].map(s=>({...BASECFG.MTF_TREND_BREAKOUT,breakoutBars:b,initialStopAtr:s}))),
 TREND_PULLBACK_RECLAIM:[.45,.60,.75].flatMap(x=>[2.0,2.25,2.5].map(s=>({...BASECFG.TREND_PULLBACK_RECLAIM,pullbackAtr:x,initialStopAtr:s}))),
 VOLATILITY_EXPANSION_TREND:[16,20,24].flatMap(b=>[.20,.25,.30].map(z=>({...BASECFG.VOLATILITY_EXPANSION_TREND,breakoutBars:b,compressionQuantile:z})))
};

const rows=new Map();for(const s of SYMBOLS){try{const r=await klines(s);if(r.length>=1000){rows.set(s,r);console.error('downloaded',s,r.length)}}catch(e){console.error('skip',s,e.message)}}
const allTimes=[...rows.values()].flatMap(r=>[r[0].t,r.at(-1).t]);const minT=Math.min(...allTimes),maxT=Math.max(...allTimes),cut=minT+(maxT-minT)*(1-OOS_FRAC);
const result={generatedAt:new Date().toISOString(),status:'RESEARCH_ONLY',protocol:'validation/evidence-gate-round4.json',cutoff:new Date(cut).toISOString(),reports:{}};
for(const name of Object.keys(BASECFG)){
 const all=[];for(const [sym,r] of rows){for(const t of simulate(name,r,BASECFG[name]))all.push({...t,sym})}all.sort((a,b)=>a.t-b.t);const oos=all.filter(x=>x.t>=cut),base=enrichMetrics(oos,BASE_COST),stress=enrichMetrics(oos,STRESS_COST),folds=foldStats(oos,BASE_COST,cut,maxT);let positiveNeighbors=0;const neighborSumm=[];
 for(const cfg of NEIGHBORS[name]){const nt=[];for(const [sym,r] of rows)for(const t of simulate(name,r,cfg))if(t.t>=cut)nt.push(t);const m=enrichMetrics(nt,BASE_COST),ok=m.expectancy>0&&m.pf>1;positiveNeighbors+=ok?1:0;neighborSumm.push({n:m.n,expectancy:m.expectancy,pf:m.pf,ok})}
 const neighborFraction=positiveNeighbors/NEIGHBORS[name].length,positiveFolds=folds.filter(x=>x.n>0&&x.expectancy>0).length;
 const pass=base.n>=GATE.minOosTrades&&base.pf>=GATE.minPF&&base.expectancy>0&&stress.expectancy>0&&positiveFolds>=GATE.minPositiveFolds&&base.maxDD<=GATE.maxDD&&neighborFraction>=GATE.minNeighborPositive;
 result.reports[name]={allTrades:all.length,oosBase:base,oosDoubleCost:stress,folds,positiveFolds,neighborFraction,neighbors:neighborSumm,discoveryPass:pass};
}
result.discoveryWinners=Object.entries(result.reports).filter(([,v])=>v.discoveryPass).map(([k])=>k);result.verdict=result.discoveryWinners.length?'PROCEED_TO_UNTOUCHED_SYMBOL_HOLDOUT':'NO_STRATEGY_APPROVED';
await fs.mkdir('validation',{recursive:true});await fs.writeFile('validation/round4-discovery.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
