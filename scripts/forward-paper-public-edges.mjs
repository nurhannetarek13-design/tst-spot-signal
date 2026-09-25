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
const symbols=(Array.isArray(manifest.symbols)&&manifest.symbols.length?manifest.symbols:[manifest.symbol]).filter(Boolean);
const scope=manifest.scope||"SINGLE_SYMBOL";
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

if(!fingerprint||!symbols.length){
  const ledger=loadLedger();
if(!Array.isArray(ledger.closed))ledger.closed=[];
if(!ledger.seen)ledger.seen={};

async function marketSnapshot(sym){
  const [book,raw]=await Promise.all([
    get(`/api/v3/ticker/bookTicker?symbol=${sym}`),
    get(`/api/v3/klines?symbol=${sym}&interval=${tf}&limit=1000`)
  ]);
  const bid=n(book.bidPrice),ask=n(book.askPrice);
  const spread=((ask-bid)/((ask+bid)/2))*100;
  return {symbol:sym,bid,ask,spread,candles:raw.map(candle)};
}

let leader3=0;
if(family==="CROSS_CRYPTO_LEAD_LAG"){
  const anchors=await Promise.all(["BTCUSDT","ETHUSDT","SOLUSDT"].map(s=>get(`/api/v3/klines?symbol=${s}&interval=1h&limit=8`)));
  const vals=anchors.map(rows=>{const x=rows.map(candle);return x.at(-1).c/x.at(-4).c-1;});
  leader3=vals.reduce((a,b)=>a+b,0)/vals.length;
}

if(ledger.open&&ledger.open.candidateFingerprint!==fingerprint){
  const snap=await marketSnapshot(ledger.open.symbol);
  const px=snap.bid,gross=STAKE*(px/ledger.open.entry-1),pnl=gross-STAKE*COST_PER_SIDE-(STAKE*(px/ledger.open.entry))*COST_PER_SIDE;
  ledger.closed.push({...ledger.open,exit:px,pnl,reason:"CANDIDATE_ROTATED",closedAt:Date.now()});
  ledger.open=null;
}

if(ledger.open){
  const snap=await marketSnapshot(ledger.open.symbol);
  const px=snap.bid,ageH=(Date.now()-ledger.open.openedAt)/3600000;let reason=null;
  if(px<=ledger.open.stop)reason="STOP";else if(px>=ledger.open.target)reason="TARGET";else if(ageH>=ledger.open.holdHours)reason="TIME";
  if(reason){
    const gross=STAKE*(px/ledger.open.entry-1),pnl=gross-STAKE*COST_PER_SIDE-(STAKE*(px/ledger.open.entry))*COST_PER_SIDE,closed={...ledger.open,exit:px,pnl,reason,closedAt:Date.now()};
    ledger.closed.push(closed);if(ledger.closed.length>1000)ledger.closed.splice(0,ledger.closed.length-1000);ledger.open=null;
    await telegram([`${pnl>=0?"✅":"🔴"} UNIFIED EDGE CLOSE — ${closed.symbol.replace("USDT","/USDT")}`,`🧠 ${family}`,`السبب: ${reason}`,`💰 PnL: ${pnl>=0?"+":""}${fmt(pnl)} USDT`].join("\n"));
  }
}

if(!ledger.open&&fingerprint){
  const opportunities=[];
  for(const sym of symbols){
    try{
      const snap=await marketSnapshot(sym);
      const sig=signal(snap.candles,snap.spread,leader3);
      if(!sig)continue;
      const barTime=snap.candles.at(-1)?.t||0;
      const seenKey=scope==="MULTI_SYMBOL_BASKET"?`${fingerprint}:${sym}:${barTime}`:fingerprint;
      const allowed=scope==="MULTI_SYMBOL_BASKET"
        ? !ledger.seen[seenKey]
        : (!ledger.seen[seenKey]||Date.now()-ledger.seen[seenKey]>6*3600000);
      if(allowed)opportunities.push({...snap,sig,seenKey,barTime});
    }catch(e){
      console.warn("forward snapshot failed",sym,String(e?.message||e).slice(0,120));
    }
  }
  opportunities.sort((a,b)=>b.sig.score-a.sig.score||a.symbol.localeCompare(b.symbol));
  const best=opportunities[0];
  if(best){
    const sl=Number(p.sl||0.03),tp=Number(p.tp||0.06),holdBars=Number(p.holdBars||24),mins=tf==="1h"?60:15;
    ledger.open={
      candidateId:manifest.candidateId,candidateFingerprint:fingerprint,
      symbol:best.symbol,family,timeframe:tf,scope,
      score:Math.round(best.sig.score),entry:best.ask,
      stop:best.ask*(1-sl),target:best.ask*(1+tp),
      holdHours:holdBars*mins/60,openedAt:Date.now(),why:best.sig.why
    };
    ledger.seen[best.seenKey]=Date.now();
    await telegram([`🟣 UNIFIED EDGE PAPER — ${best.symbol.replace("USDT","/USDT")} — SPOT`,`🧠 ${family}`,`⭐ القوة: ${Math.round(best.sig.score)}/100`,`💵 Paper: ${STAKE} USDT`,`💲 دخول: ${fmt(best.ask)}`,`🛑 Stop: ${fmt(ledger.open.stop)}`,`🎯 Target: ${fmt(ledger.open.target)}`,`🔎 ${best.sig.why}`,...(scope==="MULTI_SYMBOL_BASKET"?["🧺 Basket: مركز واحد فقط على مستوى السلة."]:[]),`⚠️ Paper only.`].join("\n"));
  }
}

const m=metrics(ledger.closed),forwardPass=m.trades>=50&&m.profitFactor>=1.15&&m.expectancyUSDT>0&&m.maxDrawdownUSDT<=1.0;
const report={
  engine:"FORWARD_PAPER",strategyId:"TST_UNIFIED_FORWARD_V1",
  status:forwardPass?"PASS":"COLLECTING",pass:forwardPass,
  candidateId:manifest.candidateId,candidateFingerprint:fingerprint,
  symbol,symbols,scope,family,timeframe:tf,
  validationScope:(manifest.validation||{}).forwardScope||"FULL_CANDIDATE",
  metrics:m,open:ledger.open,authorization:"FORWARD_PAPER_ONLY",liveTrading:false,
  generatedAt:new Date().toISOString(),
  notes:scope==="MULTI_SYMBOL_BASKET"
    ?"Global single-position basket forward paper. Scans all manifest symbols but permits only one open position across the basket. Requires 50 closed trades, PF>=1.15, positive expectancy and max DD<=1 USDT."
    :"Exact unified single-symbol candidate. Requires 50 closed forward trades, PF>=1.15, positive expectancy and max DD<=1 USDT."
};
saveJson(LEDGER_PATH,ledger);saveJson(REPORT_PATH,report);saveJson(ARTIFACT_PATH,report);console.log(JSON.stringify(report,null,2));
