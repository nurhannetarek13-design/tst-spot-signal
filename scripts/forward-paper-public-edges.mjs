#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const API="https://data-api.binance.vision";
const MAX_PRICE=3.0, MIN_QV=20_000_000, MAX_QV=150_000_000;
const STAKE=5.5, COST_PER_SIDE=0.0015;
const SCORE_MIN=88, MAX_SYMBOLS=25;
const LEDGER_PATH="paper/public-edge-forward-ledger.json";
const ARTIFACT_PATH="artifacts/public-edge-forward.json";
const MAJORS=new Set(["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"]);
const EXCLUDED=new Set(["USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT"]);

async function get(p){
  const r=await fetch(API+p,{headers:{"user-agent":"tst-public-edge-forward/1.0"}});
  if(!r.ok) throw new Error(`${r.status} ${p}`);
  return r.json();
}
function n(x){return Number(x||0)}
function ema(a,p){if(!a.length)return 0;const k=2/(p+1);let e=a[0];for(let i=1;i<a.length;i++)e=a[i]*k+e*(1-k);return e}
function rsi(a,p=14){if(a.length<p+1)return 50;let g=0,l=0;for(let i=a.length-p;i<a.length;i++){const d=a[i]-a[i-1];if(d>0)g+=d;else l-=d}if(l===0)return 100;const rs=(g/p)/(l/p);return 100-100/(1+rs)}
function median(a){const x=[...a].sort((a,b)=>a-b);if(!x.length)return 0;const m=Math.floor(x.length/2);return x.length%2?x[m]:(x[m-1]+x[m])/2}
function percentile(a,p){const x=[...a].filter(Number.isFinite).sort((a,b)=>a-b);if(!x.length)return 0;return x[Math.min(x.length-1,Math.floor((x.length-1)*p))]}
function candle(k){return{t:n(k[0]),o:n(k[1]),h:n(k[2]),l:n(k[3]),c:n(k[4]),qv:n(k[7])}}
function atrPct(c){
  const out=[];
  for(let i=14;i<c.length;i++){
    let s=0;
    for(let j=i-13;j<=i;j++){const pc=c[j-1].c,x=c[j];s+=Math.max(x.h-x.l,Math.abs(x.h-pc),Math.abs(x.l-pc))}
    out.push((s/14)/c[i].c);
  }
  return out;
}
function fmt(x){if(!Number.isFinite(x))return"-";return Math.abs(x)>=1?x.toFixed(4):x.toFixed(8).replace(/0+$/,"").replace(/\.$/,"")}
function allowedBase(base){return base&&!EXCLUDED.has(base)&&!MAJORS.has(base)&&!/(UP|DOWN|BULL|BEAR)$/.test(base)}
function loadLedger(){try{return JSON.parse(fs.readFileSync(LEDGER_PATH,"utf8"))}catch{return{open:null,closed:[],seen:{}}}}
function saveJson(p,obj){fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,JSON.stringify(obj,null,2))}
async function telegram(text){
  const token=process.env.TELEGRAM_BOT_TOKEN,chat=process.env.TELEGRAM_CHAT_ID;
  if(!token||!chat)return false;
  const r=await fetch(`https://api.telegram.org/bot${token}/sendMessage`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({chat_id:String(chat),text,disable_web_page_preview:true})});
  return r.ok;
}
async function k1h(symbol){return (await get(`/api/v3/klines?symbol=${symbol}&interval=1h&limit=800`)).map(candle)}

function signalFor(symbol,c,leader3,spreadPct){
  if(c.length<160)return [];
  const closes=c.map(x=>x.c),last=c.at(-1),qv=c.map(x=>x.qv);
  const rv=last.qv/Math.max(1,median(qv.slice(-25,-1)));
  const R=rsi(closes),e24=ema(closes,24),e48=ema(closes,48),e120=ema(closes,120);
  const ret3=last.c/closes.at(-4)-1,ret6=last.c/closes.at(-7)-1,ret24=last.c/closes.at(-25)-1;
  const atrs=atrPct(c),a=atrs.at(-1)||0;
  const out=[];

  if(last.c>e48&&e48>e120&&ret24>0.02&&a>=0.006&&a<=0.08&&rv>=0.8){
    const score=Math.min(100,80+Math.min(8,ret24*100)+Math.min(6,rv*2)+(spreadPct<=0.10?4:0));
    out.push({family:"TS_MOMENTUM",score,holdHours:24,sl:0.03,tp:0.06,why:`24h ${(ret24*100).toFixed(2)}% | RV ${rv.toFixed(2)}x`});
  }

  const gap=leader3-ret3;
  if(leader3>=0.012&&gap>=0.008&&ret3>-0.02&&last.c>e24&&rv>=0.9&&R>=42&&R<=70){
    const score=Math.min(100,82+Math.min(8,gap*500)+Math.min(6,leader3*250)+(spreadPct<=0.10?4:0));
    out.push({family:"CROSS_CRYPTO_LEAD_LAG",score,holdHours:6,sl:0.025,tp:0.05,why:`leaders 3h ${(leader3*100).toFixed(2)}% | gap ${(gap*100).toFixed(2)}%`});
  }

  const ret6s=[];
  for(let i=6;i<closes.length;i++)ret6s.push(closes[i]/closes[i-6]-1);
  const recent=ret6s.slice(-30*24),mu=recent.reduce((a,b)=>a+b,0)/Math.max(1,recent.length);
  const sd=Math.sqrt(recent.reduce((s,x)=>s+(x-mu)**2,0)/Math.max(1,recent.length));
  const z=sd>0?(ret6-mu)/sd:0;
  const detrended=last.qv/Math.max(1,median(qv.slice(-7*24)));
  if(z<=-2&&detrended<=1.10&&R<=35&&last.c<e24){
    const score=Math.min(100,82+Math.min(10,Math.abs(z)*3)+(spreadPct<=0.10?4:0));
    out.push({family:"LIQUIDITY_REVERSAL",score,holdHours:12,sl:0.03,tp:0.05,why:`6h z ${z.toFixed(2)} | RSI ${R.toFixed(1)}`});
  }

  const prevHigh=Math.max(...c.slice(-25,-1).map(x=>x.h));
  const atrWindow=atrs.slice(-72);
  const compressed=a<=percentile(atrWindow,0.25);
  if(compressed&&last.c>prevHigh&&rv>=1.5&&R>=55&&R<=75){
    const score=Math.min(100,84+Math.min(8,(rv-1)*4)+(spreadPct<=0.10?4:0));
    out.push({family:"VOLATILITY_BREAKOUT",score,holdHours:12,sl:0.025,tp:0.055,why:`RV ${rv.toFixed(2)}x | ATR compression breakout`});
  }
  return out.map(x=>({...x,symbol,price:last.c,rv,rsi:R,ret3,ret24}));
}

const ledger=loadLedger();
const [info,tickers,books]=await Promise.all([get("/api/v3/exchangeInfo"),get("/api/v3/ticker/24hr"),get("/api/v3/ticker/bookTicker")]);
const bookMap=new Map(books.map(x=>[x.symbol,x]));
const tradable=new Map(info.symbols.filter(s=>s.status==="TRADING"&&s.quoteAsset==="USDT"&&s.isSpotTradingAllowed).map(s=>[s.symbol,s]));

if(ledger.open){
  const b=bookMap.get(ledger.open.symbol);
  if(b){
    const px=n(b.bidPrice),ageH=(Date.now()-ledger.open.openedAt)/3600000;
    let reason=null;
    if(px<=ledger.open.stop)reason="STOP";
    else if(px>=ledger.open.target)reason="TARGET";
    else if(ageH>=ledger.open.holdHours)reason="TIME";
    if(reason){
      const gross=STAKE*(px/ledger.open.entry-1);
      const pnl=gross-STAKE*COST_PER_SIDE-(STAKE*(px/ledger.open.entry))*COST_PER_SIDE;
      const closed={...ledger.open,exit:px,pnl,reason,closedAt:Date.now()};
      ledger.closed.push(closed);if(ledger.closed.length>500)ledger.closed.splice(0,ledger.closed.length-500);
      ledger.open=null;
      await telegram([`${pnl>=0?"✅":"🔴"} EDGE PAPER CLOSE — ${closed.symbol.replace("USDT","/USDT")}`,`🧠 ${closed.family}`,`السبب: ${reason}`,`الدخول: ${fmt(closed.entry)} | الخروج: ${fmt(px)}`,`💰 PnL: ${pnl>=0?"+":""}${fmt(pnl)} USDT`].join("\n"));
    }
  }
}

const candidatesUniverse=[];
for(const t of tickers){
  const meta=tradable.get(t.symbol),book=bookMap.get(t.symbol);if(!meta||!book)continue;
  const base=meta.baseAsset,px=n(t.lastPrice),qv=n(t.quoteVolume),bid=n(book.bidPrice),ask=n(book.askPrice);
  if(!allowedBase(base)||!(px>0&&px<=MAX_PRICE&&qv>=MIN_QV&&qv<=MAX_QV&&ask>bid&&bid>0))continue;
  const spread=((ask-bid)/((ask+bid)/2))*100;
  candidatesUniverse.push({symbol:t.symbol,qv,px,spread,ask});
}
candidatesUniverse.sort((a,b)=>b.qv-a.qv);
const focus=candidatesUniverse.slice(0,MAX_SYMBOLS);

const leaderData=await Promise.all(["BTCUSDT","ETHUSDT","SOLUSDT"].map(k1h));
const leader3=leaderData.map(c=>c.at(-1).c/c.at(-4).c-1).reduce((a,b)=>a+b,0)/3;

const all=[];
for(let i=0;i<focus.length;i+=5){
  const chunk=focus.slice(i,i+5);
  const sets=await Promise.all(chunk.map(async x=>({x,c:await k1h(x.symbol)})));
  for(const {x,c} of sets)all.push(...signalFor(x.symbol,c,leader3,x.spread).map(s=>({...s,entry:x.ask,spreadPct:x.spread,qv:x.qv})));
}
all.sort((a,b)=>b.score-a.score);

if(!ledger.open){
  const best=all.find(x=>x.score>=SCORE_MIN && (!ledger.seen[x.symbol+":"+x.family] || Date.now()-ledger.seen[x.symbol+":"+x.family]>6*3600000));
  if(best){
    ledger.open={symbol:best.symbol,family:best.family,score:Math.round(best.score),entry:best.entry,stop:best.entry*(1-best.sl),target:best.entry*(1+best.tp),holdHours:best.holdHours,openedAt:Date.now(),why:best.why};
    ledger.seen[best.symbol+":"+best.family]=Date.now();
    await telegram([`🟣 EDGE PAPER SIGNAL — ${best.symbol.replace("USDT","/USDT")} — SPOT`,`🧠 Edge: ${best.family}`,`⭐ القوة: ${Math.round(best.score)}/100`,`💵 Paper: ${STAKE} USDT`,`💲 دخول: ${fmt(best.entry)}`,`🛑 Stop: ${fmt(ledger.open.stop)}`,`🎯 Target: ${fmt(ledger.open.target)}`,`🔎 ${best.why}`,`⚠️ Research/Paper only — مش Live approval.`].join("\n"));
  }
}

saveJson(LEDGER_PATH,ledger);
saveJson(ARTIFACT_PATH,{engine:"PUBLIC_EDGE_FORWARD",mode:"PAPER_ONLY",liveTrading:false,leader3h:leader3,universe:focus,signals:all.slice(0,20),open:ledger.open,closedCount:ledger.closed.length,generatedAt:new Date().toISOString()});
console.log(JSON.stringify({open:ledger.open,signals:all.slice(0,5),closedCount:ledger.closed.length},null,2));
