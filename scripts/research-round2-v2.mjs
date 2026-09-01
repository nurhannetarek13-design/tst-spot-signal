import fs from 'node:fs/promises';

const BASES=['https://api.binance.com','https://api1.binance.com','https://api2.binance.com','https://data-api.binance.vision'];
const DISCOVERY=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','NEARUSDT','UNIUSDT','AAVEUSDT','ETCUSDT','FILUSDT','ATOMUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT'];
const DAYS=420, CAPITAL=20.12, QUOTE=7, STOP_PCT=0.02;
const BASE_COST=2*(0.001+0.0005)+0.0006, STRESS_COST=2*(0.002+0.001)+0.0012, FDR=0.10;
const EXCLUDED=new Set(['USDC','FDUSD','TUSD','USDP','DAI','EUR','TRY','BRL','GBP','AUD','BIDR','IDRT','UAH','NGN','RUB','ZAR']);

async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path,{signal:AbortSignal.timeout(20000)});if(r.ok)return await r.json();last=`${b}:${r.status}`;}catch(e){last=e.message;}}throw new Error(last||path);}
async function klines(symbol){const out=[];let cur=Date.now()-DAYS*86400000;while(cur<Date.now()){const p=await get(`/api/v3/klines?symbol=${symbol}&interval=1h&limit=1000&startTime=${cur}`);if(!p.length)break;for(const k of p)out.push({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],qv:+k[7],closeTime:+k[6]});const n=+p.at(-1)[6]+1;if(n<=cur)break;cur=n;if(p.length<1000)break;}return out.filter(x=>x.closeTime<Date.now());}
const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:0;
function sd(a){if(a.length<2)return 0;const m=mean(a);return Math.sqrt(a.reduce((s,x)=>s+(x-m)**2,0)/(a.length-1));}
function normCdf(x){const t=1/(1+0.2316419*Math.abs(x)),d=0.3989423*Math.exp(-x*x/2),p=d*t*(0.3193815+t*(-0.3565638+t*(1.781478+t*(-1.821256+t*1.330274))));return x>0?1-p:p;}
function stats(x){const m=mean(x),s=sd(x),se=x.length?s/Math.sqrt(x.length):0,z=se?m/se:0,p=x.length>=10?2*(1-normCdf(Math.abs(z))):1;return {n:x.length,mean:+m.toFixed(8),sd:+s.toFixed(8),z:+z.toFixed(3),p:+Math.min(1,p).toFixed(6),hitRate:+(x.length?x.filter(v=>v>0).length/x.length:0).toFixed(4)};}
function bh(items){const sorted=[...items].sort((a,b)=>a.stats.p-b.stats.p);let k=-1;for(let i=0;i<sorted.length;i++)if(sorted[i].stats.p<=((i+1)/sorted.length)*FDR)k=i;const keep=new Set(k>=0?sorted.slice(0,k+1).map(x=>x.id):[]);return items.map(x=>({...x,bhSignificant:keep.has(x.id)}));}
const ret=(r,i,n)=>i>=n?r[i].c/r[i-n].c-1:NaN, fwd=(r,i,n)=>i+n<r.length?r[i+n].c/r[i].c-1:NaN;
function zscore(vals,x){const s=sd(vals);return s?(x-mean(vals))/s:0;}
async function selectHoldout(){const [info,t]=await Promise.all([get('/api/v3/exchangeInfo'),get('/api/v3/ticker/24hr')]);const ok=new Set(info.symbols.filter(x=>x.status==='TRADING'&&x.quoteAsset==='USDT'&&!EXCLUDED.has(x.baseAsset)&&!/(UP|DOWN|BULL|BEAR)$/.test(x.baseAsset)).map(x=>x.symbol));return t.filter(x=>ok.has(x.symbol)&&!DISCOVERY.includes(x.symbol)).sort((a,b)=>+b.quoteVolume-+a.quoteVolume).slice(0,8).map(x=>x.symbol);}
function buildStudies(symbolRows){
  const btc=symbolRows.get('BTCUSDT');
  if(!btc) return [];
  const studies=[{id:'BTC_LEAD_POS_4H',h:4,events:[]},{id:'RS_12H_CONT_4H',h:4,events:[]},{id:'EXTREME_VOLUME_UP_CONT_4H',h:4,events:[]},{id:'EXTREME_DOWN_REVERSAL_4H',h:4,events:[]},{id:'VOL_EXPANSION_UP_CONT_4H',h:4,events:[]},{id:'TREND_24H_CONT_12H',h:12,events:[]}];
  const btcMap=new Map(btc.map((x,i)=>[x.t,i]));
  for(const [sym,r] of symbolRows){
    if(sym==='BTCUSDT')continue;
    for(let i=72;i<r.length-13;i++){
      const bi=btcMap.get(r[i].t); if(bi==null||bi<48||bi+4>=btc.length)continue;
      const future4=fwd(r,i,4),future12=fwd(r,i,12),bret=ret(btc,bi,1),bhist=[];
      for(let j=bi-48;j<bi;j++)bhist.push(ret(btc,j,1));
      if(zscore(bhist,bret)>=1.5&&bret>0)studies[0].events.push({sym,t:r[i].t,raw:future4});
      const r12=ret(r,i,12); if(r12>0.03)studies[1].events.push({sym,t:r[i].t,raw:future4});
      const qhist=r.slice(i-48,i).map(x=>x.qv),qz=zscore(qhist,r[i].qv),r1=ret(r,i,1);
      if(qz>=2&&r1>0.01)studies[2].events.push({sym,t:r[i].t,raw:future4});
      const rh=[];for(let j=i-48;j<i;j++)rh.push(ret(r,j,1));
      if(zscore(rh,r1)<=-2.5)studies[3].events.push({sym,t:r[i].t,raw:future4});
      const short=sd(Array.from({length:6},(_,k)=>ret(r,i-k,1))),long=sd(Array.from({length:48},(_,k)=>ret(r,i-k,1)));
      if(long>0&&short/long>=1.6&&r1>0)studies[4].events.push({sym,t:r[i].t,raw:future4});
      const r24=ret(r,i,24); if(r24>0.05)studies[5].events.push({sym,t:r[i].t,raw:future12});
    }
  }
  return studies.map(x=>({...x,stats:stats(x.events.map(e=>e.raw))}));
}
function simulateCandidate(study,symbolRows,cost,cap=1){
  const events=[...study.events].sort((a,b)=>a.t-b.t),active=[];let cash=CAPITAL,peak=CAPITAL,maxDD=0;const trades=[];
  for(const e of events){
    for(let k=active.length-1;k>=0;k--)if(active[k].exitT<=e.t){cash+=active[k].pnl;active.splice(k,1);}
    if(active.length>=cap||cash<QUOTE+2)continue;
    const r=symbolRows.get(e.sym); if(!r)continue; const idx=r.findIndex(x=>x.t===e.t); if(idx<0||idx+study.h>=r.length)continue;
    const entry=r[idx+1]?.o??r[idx].c; let exit=r[idx+study.h].c,reason='TIME';
    for(let j=idx+1;j<=idx+study.h;j++){if(r[j].l<=entry*(1-STOP_PCT)){exit=entry*(1-STOP_PCT);reason='STOP';break;}}
    const net=QUOTE*(exit/entry-1)-QUOTE*cost; trades.push({t:e.t,sym:e.sym,pnl:net,reason}); active.push({exitT:r[idx+study.h].t,pnl:net});
    const equity=cash+active.reduce((s,x)=>s+x.pnl,0); peak=Math.max(peak,equity); maxDD=Math.max(maxDD,(peak-equity)/peak);
  }
  const ps=trades.map(x=>x.pnl),w=ps.filter(x=>x>0),l=ps.filter(x=>x<0),sum=a=>a.reduce((s,x)=>s+x,0);
  return {trades:ps.length,expectancy:+mean(ps).toFixed(5),profitFactor:+(l.length?sum(w)/Math.abs(sum(l)):0).toFixed(3),netPnl:+sum(ps).toFixed(4),maxDrawdownPct:+(maxDD*100).toFixed(3),topTwoProfitShare:+(sum(w)>0?sum([...w].sort((a,b)=>b-a).slice(0,2))/sum(w):1).toFixed(3)};
}
function folds(study,rows,cost){const ts=study.events.map(e=>e.t).sort((a,b)=>a-b);if(!ts.length)return[];const lo=ts[0],hi=ts.at(-1),span=(hi-lo)/5;return Array.from({length:5},(_,i)=>simulateCandidate({...study,events:study.events.filter(e=>e.t>=lo+i*span&&e.t<(i===4?hi+1:lo+(i+1)*span))},rows,cost,1));}

const holdout=await selectHoldout();const allSyms=[...new Set([...DISCOVERY,...holdout])],rows=new Map();
for(const s of allSyms){try{rows.set(s,await klines(s));console.error('downloaded',s,rows.get(s).length);}catch(e){console.error('skip',s,e.message);}}
const discoveryRows=new Map(DISCOVERY.filter(s=>rows.has(s)).map(s=>[s,rows.get(s)]));
const studies=bh(buildStudies(discoveryRows));const candidates=[];
for(const st of studies){
  if(!st.bhSignificant||st.stats.mean<=0)continue;
  const base=simulateCandidate(st,discoveryRows,BASE_COST,1),stress=simulateCandidate(st,discoveryRows,STRESS_COST,1),wf=folds(st,discoveryRows,BASE_COST);
  candidates.push({id:st.id,eventStudy:st.stats,base,stress,walkForward:wf,discoveryGate:{minTrades:base.trades>=100,pf:base.profitFactor>=1.15,positiveBase:base.expectancy>0,positiveStress:stress.expectancy>0,foldsPositive:wf.filter(x=>x.expectancy>0).length>=4,maxDD:base.maxDrawdownPct<=20}});
}
const holdRows=new Map(holdout.filter(s=>rows.has(s)).map(s=>[s,rows.get(s)]));
for(const c of candidates){if(holdRows.size){const hr=new Map([['BTCUSDT',rows.get('BTCUSDT')],...holdRows]);const hs=buildStudies(hr).find(x=>x.id===c.id);c.symbolHoldout=hs?simulateCandidate(hs,holdRows,BASE_COST,1):null;}}
const out={generatedAt:new Date().toISOString(),protocol:'validation/evidence-gate-round2.json',status:'RESEARCH_ONLY',liveTrading:false,validatedStrategyRelease:null,discoverySymbols:[...discoveryRows.keys()],symbolHoldout:[...holdRows.keys()],costs:{baseRoundTrip:BASE_COST,stressRoundTrip:STRESS_COST},eventStudies:studies.map(({events,...x})=>x),candidates,verdict:candidates.some(c=>Object.values(c.discoveryGate).every(Boolean)&&c.symbolHoldout?.expectancy>0)?'CANDIDATE_SURVIVED_DISCOVERY_NOT_PAPER_APPROVED':'NO_DISCOVERY_CANDIDATE_PASSED'};
await fs.mkdir('validation',{recursive:true});await fs.writeFile('validation/research-round2.json',JSON.stringify(out,null,2));console.log(JSON.stringify(out,null,2));