#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const API="https://data-api.binance.vision";
const MANIFEST_PATH="validation/fusion/candidate-manifest.json";
const LEDGER_PATH="paper/public-edge-forward-ledger.json";
const REPORT_PATH="validation/fusion/forward-latest.json";
const ARTIFACT_PATH="artifacts/public-edge-forward.json";
const STAKE=5.5,COST_PER_SIDE=0.0015;

const manifest=JSON.parse(fs.readFileSync(MANIFEST_PATH,"utf8"));
const p=manifest.params||{};
const symbol=manifest.symbol;
const family=manifest.family;
const tf=manifest.timeframe||"1h";
const fingerprint=manifest.candidateFingerprint;

async function get(x){const r=await fetch(API+x,{headers:{"user-agent":"tst-unified-forward/1.0"}});if(!r.ok)throw new Error(`${r.status} ${x}`);return r.json()}
function n(x){return Number(x||0)}
function candle(k){return{t:n(k[0]),o:n(k[1]),h:n(k[2]),l:n(k[3]),c:n(k[4]),qv:n(k[7])}}
function ema(a,w){if(!a.length)return 0;const k=2/(w+1);let e=a[0];for(let i=1;i<a.length;i++)e=a[i]*k+e*(1-k);return e}
function rsi(a,w=14){if(a.length<w+1)return 50;let g=0,l=0;for(let i=a.length-w;i<a.length;i++){const d=a[i]-a[i-1];if(d>0)g+=d;else l-=d}if(l===0)return 100;const rs=(g/w)/(l/w);return 100-100/(1+rs)}
function median(a){const x=[...a].sort((a,b)=>a-b);if(!x.length)return 0;const m=Math.floor(x.length/2);return x.length%2?x[m]:(x[m-1]+x[m])/2}
function percentile(a,q){const x=[...a].filter(Number.isFinite).sort((a,b)=>a-b);if(!x.length)return 0;return x[Math.min(x.length-1,Math.floor((x.length-1)*q))]}
function atrPctSeries(c){const out=[];for(let i=14;i<c.length;i++){let s=0;for(let j=i-13;j<=i;j++){const pc=c[j-1].c,x=c[j];s+=Math.max(x.h-x.l,Math.abs(x.h-pc),Math.abs(x.l-pc))}out.push((s/14)/c[i].c)}return out}
function fmt(x){if(!Number.isFinite(x))return"-";return Math.abs(x)>=1?x.toFixed(4):x.toFixed(8).replace(/0+$/,"").replace(/\.$/,"")}
function loadLedger(){try{return JSON.parse(fs.readFileSync(LEDGER_PATH,"utf8"))}catch{return{open:null,closed:[],seen:{}}}}
function saveJson(pth,obj){fs.mkdirSync(path.dirname(pth),{recursive:true});fs.writeFileSync(pth,JSON.stringify(obj,null,2))}
async function telegram(text){const token=process.env.TELEGRAM_BOT_TOKEN,chat=process.env.TELEGRAM_CHAT_ID;if(!token||!chat)return false;const r=await fetch(`https://api.telegram.org/bot${token}/sendMessage`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({chat_id:String(chat),text,disable_web_page_preview:true})});return r.ok}

if(!fingerprint||!symbol){
  const ledger=loadLedger();
  const report={engine:"FORWARD_PAPER",strategyId:"TST_UNIFIED_FORWARD_V1",status:"NO_CANDIDATE",pass:false,candidateId:null,candidateFingerprint:null,metrics:{trades:0,wins:0,winRate:0,netPnlUSDT:0,expectancyUSDT:0,profitFactor:0,maxDrawdownUSDT:0},open:null,authorization:"FORWARD_PAPER_ONLY",liveTrading:false,generatedAt:new Date().toISOString(),notes:"No unified candidate is currently eligible; forward tracker is fail-closed."};
  saveJson(LEDGER_PATH,ledger);saveJson(REPORT_PATH,report);saveJson(ARTIFACT_PATH,report);
  console.log(JSON.stringify(report,null,2));
  process.exit(0);
}

function signal(c,spreadPct){
  if(c.length<160)return null;
  const closes=c.map(x=>x.c),qv=c.map(x=>x.qv),last=c.at(-1),R=rsi(closes),rv=last.qv/Math.max(1,median(qv.slice(-25,-1))),atrs=atrPctSeries(c),a=atrs.at(-1)||0;
  let ok=false,why="",score=80;
  if(family==="TS_MOMENTUM"){
    const ef=ema(closes,Number(p.emaFast||48)),es=ema(closes,Number(p.emaSlow||120)),lb=Number(p.retLookback||24),ret=last.c/closes.at(-lb-1)-1;
    ok=last.c>ef&&ef>es&&ret>Number(p.retMin||0.02)&&a>=Number(p.atrMin||0.006)&&a<=Number(p.atrMax||0.08)&&rv>=Number(p.relvol||0.8);
    score=Math.min(100,80+Math.min(8,ret*100)+Math.min(6,rv*2)+(spreadPct<=0.10?4:0));why=`ret${lb} ${(ret*100).toFixed(2)}% | RV ${rv.toFixed(2)}x`;
  } else if(family==="LIQUIDITY_REVERSAL"){
    const lb=Number(p.retLookback||6),zlb=Number(p.zLookback||720),rr=[];for(let i=Math.max(lb,closes.length-zlb);i<closes.length;i++)rr.push(closes[i]/closes[i-lb]-1);
    const mu=rr.reduce((a,b)=>a+b,0)/Math.max(1,rr.length),sd=Math.sqrt(rr.reduce((s,x)=>s+(x-mu)**2,0)/Math.max(1,rr.length)),cur=last.c/closes.at(-lb-1)-1,z=sd>0?(cur-mu)/sd:0,vr=last.qv/Math.max(1,median(qv.slice(-7*24)));
    ok=z<=Number(p.zMax||-2)&&vr<=Number(p.volumeRatioMax||1.10)&&R<=Number(p.rsiMax||35)&&last.c<ema(closes,24);score=Math.min(100,82+Math.min(10,Math.abs(z)*3)+(spreadPct<=0.10?4:0));why=`z ${z.toFixed(2)} | RSI ${R.toFixed(1)}`;
  } else if(family==="VOLATILITY_BREAKOUT"){
    const lb=Number(p.lookback||24),clb=Number(p.compressionLookback||72),hh=Math.max(...c.slice(-lb-1,-1).map(x=>x.h)),rank=atrs.slice(-clb),compressed=a<=percentile(rank,Number(p.compressionPct||0.25));
    ok=compressed&&last.c>hh&&rv>=Number(p.relvol||1.5)&&R>=Number(p.rsiMin||55)&&R<=Number(p.rsiMax||75);score=Math.min(100,84+Math.min(8,(rv-1)*4)+(spreadPct<=0.10?4:0));why=`RV ${rv.toFixed(2)}x | compression breakout`;
  } else if(family==="TREND_BREAKOUT"){
    const ef=ema(closes,Number(p.fast||20)),es=ema(closes,Number(p.slow||60)),lb=Number(p.lookback||20),hh=Math.max(...c.slice(-lb-1,-1).map(x=>x.h));
    ok=ef>es&&last.c>hh&&rv>=Number(p.relvol||1.0)&&R>=52&&R<=72;score=88;why=`trend breakout | RV ${rv.toFixed(2)}x`;
  } else if(family==="MEAN_REVERSION"){
    const x=closes.slice(-20),mid=x.reduce((a,b)=>a+b,0)/x.length,sd=Math.sqrt(x.reduce((s,v)=>s+(v-mid)**2,0)/x.length),lower=mid-Number(p.bb||2)*sd;
    ok=last.c<lower&&R<=Number(p.rsi_in||34)&&rv>=0.75;score=88;why=`mean reversion | RSI ${R.toFixed(1)}`;
  } else if(family==="VOLATILITY_MOMENTUM"){
    const lb=Number(p.lookback||20),hh=Math.max(...c.slice(-lb-1,-1).map(x=>x.h)),e20=ema(closes,20);
    ok=last.c>hh&&rv>=Number(p.relvol||1.4)&&R>=Number(p.rsi_min||52)&&R<=74&&last.c>e20;score=90;why=`vol momentum | RV ${rv.toFixed(2)}x`;
  }
  return ok?{score,why,close:last.c}:null;
}

function metrics(closed){
  const rows=closed.filter(x=>x.candidateFingerprint===fingerprint);const pnls=rows.map(x=>Number(x.pnl||0));const n=pnls.length;let gp=0,gl=0,eq=0,peak=0,dd=0,wins=0;
  for(const x of pnls){if(x>0){gp+=x;wins++}else gl+=Math.abs(x);eq+=x;peak=Math.max(peak,eq);dd=Math.max(dd,peak-eq)}
  const net=pnls.reduce((a,b)=>a+b,0),pf=gl>0?gp/gl:(gp>0?999:0);
  return{trades:n,wins,winRate:n?wins/n:0,netPnlUSDT:net,expectancyUSDT:n?net/n:0,profitFactor:pf,maxDrawdownUSDT:dd}
}

const ledger=loadLedger();
if(!Array.isArray(ledger.closed))ledger.closed=[];
if(!ledger.seen)ledger.seen={};
const [book,raw]=await Promise.all([get(`/api/v3/ticker/bookTicker?symbol=${symbol}`),get(`/api/v3/klines?symbol=${symbol}&interval=${tf}&limit=1000`)]);
const bid=n(book.bidPrice),ask=n(book.askPrice),spread=((ask-bid)/((ask+bid)/2))*100,c=raw.map(candle);

if(ledger.open&&ledger.open.candidateFingerprint!==fingerprint){
  const gross=STAKE*(bid/ledger.open.entry-1),pnl=gross-STAKE*COST_PER_SIDE-(STAKE*(bid/ledger.open.entry))*COST_PER_SIDE;
  ledger.closed.push({...ledger.open,exit:bid,pnl,reason:"CANDIDATE_ROTATED",closedAt:Date.now()});ledger.open=null;
}

if(ledger.open){
  const px=bid,ageH=(Date.now()-ledger.open.openedAt)/3600000;let reason=null;
  if(px<=ledger.open.stop)reason="STOP";else if(px>=ledger.open.target)reason="TARGET";else if(ageH>=ledger.open.holdHours)reason="TIME";
  if(reason){
    const gross=STAKE*(px/ledger.open.entry-1),pnl=gross-STAKE*COST_PER_SIDE-(STAKE*(px/ledger.open.entry))*COST_PER_SIDE,closed={...ledger.open,exit:px,pnl,reason,closedAt:Date.now()};
    ledger.closed.push(closed);if(ledger.closed.length>1000)ledger.closed.splice(0,ledger.closed.length-1000);ledger.open=null;
    await telegram([`${pnl>=0?"✅":"🔴"} UNIFIED EDGE CLOSE — ${symbol.replace("USDT","/USDT")}`,`🧠 ${family}`,`السبب: ${reason}`,`💰 PnL: ${pnl>=0?"+":""}${fmt(pnl)} USDT`].join("\n"));
  }
}

if(!ledger.open&&fingerprint){
  const sig=signal(c,spread),seenKey=fingerprint;
  if(sig&&(!ledger.seen[seenKey]||Date.now()-ledger.seen[seenKey]>6*3600000)){
    const sl=Number(p.sl||0.03),tp=Number(p.tp||0.06),holdBars=Number(p.holdBars||24),mins=tf==="1h"?60:15;
    ledger.open={candidateId:manifest.candidateId,candidateFingerprint:fingerprint,symbol,family,timeframe:tf,score:Math.round(sig.score),entry:ask,stop:ask*(1-sl),target:ask*(1+tp),holdHours:holdBars*mins/60,openedAt:Date.now(),why:sig.why};
    ledger.seen[seenKey]=Date.now();
    await telegram([`🟣 UNIFIED EDGE PAPER — ${symbol.replace("USDT","/USDT")} — SPOT`,`🧠 ${family}`,`⭐ القوة: ${Math.round(sig.score)}/100`,`💵 Paper: ${STAKE} USDT`,`💲 دخول: ${fmt(ask)}`,`🛑 Stop: ${fmt(ledger.open.stop)}`,`🎯 Target: ${fmt(ledger.open.target)}`,`🔎 ${sig.why}`,`⚠️ Paper only.`].join("\n"));
  }
}

const m=metrics(ledger.closed),forwardPass=m.trades>=50&&m.profitFactor>=1.15&&m.expectancyUSDT>0&&m.maxDrawdownUSDT<=1.0;
const report={engine:"FORWARD_PAPER",strategyId:"TST_UNIFIED_FORWARD_V1",status:forwardPass?"PASS":"COLLECTING",pass:forwardPass,candidateId:manifest.candidateId,candidateFingerprint:fingerprint,symbol,family,timeframe:tf,metrics:m,open:ledger.open,authorization:"FORWARD_PAPER_ONLY",liveTrading:false,generatedAt:new Date().toISOString(),notes:"Exact unified candidate. Requires 50 closed forward trades, PF>=1.15, positive expectancy and max DD<=1 USDT."};
saveJson(LEDGER_PATH,ledger);saveJson(REPORT_PATH,report);saveJson(ARTIFACT_PATH,report);console.log(JSON.stringify(report,null,2));
