import fs from 'node:fs/promises';
const BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision'];
const SYMBOLS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT'];
const DAYS=1825,OOS=.30,BASE=.003,STRESS=.006,ACCOUNT=20.12,ORDER=7;
const G={minN:100,minPF:1.15,minFolds:4,maxDD:.20,minNbr:.70};
const CFG={
 DAILY_EMA_RECLAIM:{stopAtr:2,trailAtr:3,maxHoldDays:120,rsiUpper:70},
 DAILY_DONCHIAN_TREND:{entryBars:55,exitBars:20,stopAtr:2.5,trailAtr:3.5,maxHoldDays:180},
 DAILY_MOMENTUM_BREAKOUT:{breakoutBars:20,momentumBars:90,stopAtr:2,trailAtr:3,maxHoldDays:90}
};
async function get(p){let last;for(const b of BASES){try{const r=await fetch(b+p,{signal:AbortSignal.timeout(25000)});if(r.ok)return r.json();last=`${b}:${r.status}`}catch(e){last=e.message}}throw new Error(last||p)}
async function klines(s){const out=[];let cur=Date.now()-DAYS*864e5,now=Date.now();while(cur<now){const p=await get(`/api/v3/klines?symbol=${s}&interval=1d&limit=1000&startTime=${cur}`);if(!p.length)break;for(const k of p)out.push({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],ct:+k[6]});const n=+p.at(-1)[6]+1;if(n<=cur)break;cur=n;if(p.length<1000)break}return out.filter(x=>x.ct<now)}
const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:0;
function ema(v,p){const o=new Array(v.length).fill(NaN);if(v.length<p)return o;let m=mean(v.slice(0,p));o[p-1]=m;const k=2/(p+1);for(let i=p;i<v.length;i++){m=v[i]*k+m*(1-k);o[i]=m}return o}
function atr(r,p=14){const tr=r.map((x,i)=>i?Math.max(x.h-x.l,Math.abs(x.h-r[i-1].c),Math.abs(x.l-r[i-1].c)):x.h-x.l);return ema(tr,p)}
function rsi(r,p=14){const o=new Array(r.length).fill(NaN);if(r.length<=p)return o;let g=0,l=0;for(let i=1;i<=p;i++){const d=r[i].c-r[i-1].c;if(d>=0)g+=d;else l-=d}g/=p;l/=p;o[p]=l===0?100:100-100/(1+g/l);for(let i=p+1;i<r.length;i++){const d=r[i].c-r[i-1].c;g=(g*(p-1)+Math.max(d,0))/p;l=(l*(p-1)+Math.max(-d,0))/p;o[i]=l===0?100:100-100/(1+g/l)}return o}
function prep(r){const c=r.map(x=>x.c);return {e50:ema(c,50),e200:ema(c,200),a:atr(r,14),rs:rsi(r,14),index:new Map(r.map((x,i)=>[x.t,i]))}}
function dd(rets){let eq=ACCOUNT,pk=ACCOUNT,m=0;for(const x of rets){eq+=ORDER*x;pk=Math.max(pk,eq);if(eq<=0)return 1;m=Math.max(m,1-eq/pk)}return m}
function met(tr,cost){const r=tr.map(x=>x.gross-cost),w=r.filter(x=>x>0),l=r.filter(x=>x<0),gp=w.reduce((s,x)=>s+x,0),gl=Math.abs(l.reduce((s,x)=>s+x,0)),ps=[...w].sort((a,b)=>b-a);return {n:r.length,expectancy:mean(r),pf:gl?gp/gl:(gp>0?999:0),winRate:r.length?w.length/r.length:0,maxDD:dd(r),top2Share:gp?ps.slice(0,2).reduce((s,x)=>s+x,0)/gp:0}}
function folds(tr,cost,start,end){const w=(end-start)/5,o=[];for(let f=0;f<5;f++){const a=start+f*w,b=f===4?end+1:start+(f+1)*w,t=tr.filter(x=>x.t>=a&&x.t<b);o.push({n:t.length,expectancy:mean(t.map(x=>x.gross-cost))})}return o}
let BTC=null,BP=null;
function btcRiskOn(t){if(!BTC)return true;const i=BP.index.get(t);return i!=null&&i>=200&&BTC[i].c>BP.e200[i]}
function sig(name,r,p,i,cfg){if(i<220||!Number.isFinite(p.a[i]))return false;
 if(name==='DAILY_EMA_RECLAIM')return p.e50[i]>p.e200[i]&&r[i].c>p.e200[i]&&p.e50[i]>p.e50[i-5]&&r[i-1].c<=p.e50[i-1]&&r[i].c>p.e50[i]&&p.rs[i]>=50&&p.rs[i]<=cfg.rsiUpper;
 if(name==='DAILY_DONCHIAN_TREND'){const hi=Math.max(...r.slice(i-cfg.entryBars,i).map(x=>x.h));return p.e50[i]>p.e200[i]&&r[i].c>p.e200[i]&&r[i].c>hi}
 if(name==='DAILY_MOMENTUM_BREAKOUT'){const hi=Math.max(...r.slice(i-cfg.breakoutBars,i).map(x=>x.h)),mom=r[i].c/r[i-cfg.momentumBars].c-1,r20=r[i].c/r[i-20].c-1;return r[i].c>p.e200[i]&&mom>0&&r20>0&&btcRiskOn(r[i].t)&&r[i].c>hi}
 return false}
function sim(name,r,p,cfg){const tr=[];let pos=null;for(let i=220;i<r.length-1;i++){
 if(pos){const b=r[i];if(b.l<=pos.stop){tr.push({t:b.t,gross:pos.stop/pos.entry-1});pos=null;continue}pos.peak=Math.max(pos.peak,b.h);pos.stop=Math.max(pos.stop,pos.peak-cfg.trailAtr*p.a[i]);pos.held++;
 let exit=false;if(name==='DAILY_EMA_RECLAIM')exit=b.c<p.e50[i];else if(name==='DAILY_DONCHIAN_TREND'){const lo=Math.min(...r.slice(Math.max(0,i-cfg.exitBars),i).map(x=>x.l));exit=b.c<lo}else exit=i>=20&&(b.c/r[i-20].c-1)<0;
 if(exit||pos.held>=cfg.maxHoldDays){tr.push({t:b.t,gross:b.c/pos.entry-1});pos=null;continue}}
 if(!pos&&sig(name,r,p,i,cfg)){const en=r[i+1].o,aa=p.a[i];if(en>0&&aa>0)pos={entry:en,stop:en-cfg.stopAtr*aa,peak:en,held:0}}
 }return tr}
function neighbors(name){const o=[];if(name==='DAILY_EMA_RECLAIM')for(const s of [1.75,2,2.25])for(const t of [2.75,3,3.25])for(const u of [65,70,75])o.push({...CFG[name],stopAtr:s,trailAtr:t,rsiUpper:u});if(name==='DAILY_DONCHIAN_TREND')for(const e of [45,55,65])for(const x of [15,20,25])for(const t of [3,3.5,4])o.push({...CFG[name],entryBars:e,exitBars:x,trailAtr:t});if(name==='DAILY_MOMENTUM_BREAKOUT')for(const b of [15,20,25])for(const m of [60,90,120])for(const t of [2.75,3,3.25])o.push({...CFG[name],breakoutBars:b,momentumBars:m,trailAtr:t});return o}
const rows=new Map(),preps=new Map();for(const s of SYMBOLS){try{const r=await klines(s);if(r.length>400){rows.set(s,r);preps.set(s,prep(r));console.error('downloaded',s,r.length)}}catch(e){console.error('skip',s,e.message)}}BTC=rows.get('BTCUSDT');BP=preps.get('BTCUSDT');if(!BTC)throw new Error('BTC missing');
const minT=Math.min(...[...rows.values()].map(r=>r[0].t)),maxT=Math.max(...[...rows.values()].map(r=>r.at(-1).t)),cut=minT+(maxT-minT)*(1-OOS);const res={generatedAt:new Date().toISOString(),status:'RESEARCH_ONLY',protocol:'validation/evidence-gate-round7.json',cutoff:new Date(cut).toISOString(),reports:{}};
for(const name of Object.keys(CFG)){const all=[];for(const [sym,r] of rows)for(const t of sim(name,r,preps.get(sym),CFG[name]))all.push({...t,sym});all.sort((a,b)=>a.t-b.t);const oos=all.filter(x=>x.t>=cut),base=met(oos,BASE),stress=met(oos,STRESS),fs=folds(oos,BASE,cut,maxT),pfolds=fs.filter(x=>x.n>0&&x.expectancy>0).length;let pos=0,n=0;for(const cfg of neighbors(name)){const nt=[];for(const [sym,r] of rows)for(const t of sim(name,r,preps.get(sym),cfg))if(t.t>=cut)nt.push(t);const m=met(nt,BASE),ok=m.expectancy>0&&m.pf>1;pos+=ok?1:0;n++}const frac=pos/n,pass=base.n>=G.minN&&base.pf>=G.minPF&&base.expectancy>0&&stress.expectancy>0&&pfolds>=G.minFolds&&base.maxDD<=G.maxDD&&frac>=G.minNbr;res.reports[name]={allTrades:all.length,oosBase:base,oosDoubleCost:stress,folds:fs,positiveFolds:pfolds,neighborFraction:frac,discoveryPass:pass}}
res.discoveryWinners=Object.entries(res.reports).filter(([,v])=>v.discoveryPass).map(([k])=>k);res.verdict=res.discoveryWinners.length?'PROCEED_TO_UNTOUCHED_SYMBOL_HOLDOUT':'NO_STRATEGY_APPROVED';await fs.mkdir('validation',{recursive:true});await fs.writeFile('validation/round7-discovery.json',JSON.stringify(res,null,2));console.log(JSON.stringify(res,null,2));
