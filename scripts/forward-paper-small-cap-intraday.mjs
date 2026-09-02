import fs from 'node:fs';
import { scoreCandidate, STRATEGY_ID, supportedBase } from '../src/strategies/small-cap-intraday.mjs';

const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
const LEDGER='paper/small-cap-intraday-ledger.json';
const ART='artifacts/forward-paper-small-cap-intraday.json';
const MAX_POSITIONS=2;
const RESERVE_USDT=2;
const DAILY_LOSS_LIMIT=0.50;
const ROUND_TRIP_COST=0.0036;

async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return r.json();last=new Error(`${b} ${r.status}`)}catch(e){last=e}}throw last||new Error('NO_MARKET_DATA')}
async function candles(symbol,interval,limit){const x=await get(`/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10],closeTime:+k[6]}));}
function closedOnly(c){const now=Date.now();return c.filter(x=>x.closeTime<now);}
function flow(c){const recent=c.slice(-4);const q=recent.reduce((a,x)=>a+x.q,0),tb=recent.reduce((a,x)=>a+x.tbq,0);const hist=c.slice(-24,-4);const avg=hist.reduce((a,x)=>a+x.q,0)/Math.max(1,hist.length);const now=recent.reduce((a,x)=>a+x.q,0)/Math.max(1,recent.length);return {takerBuyRatio:q?tb/q:0,relativeVolume:avg?now/avg:0};}
function chunks(a,n){const o=[];for(let i=0;i<a.length;i+=n)o.push(a.slice(i,i+n));return o;}
function utcDay(ts=Date.now()){return new Date(ts).toISOString().slice(0,10);}
function loadLedger(){const x=JSON.parse(fs.readFileSync(LEDGER,'utf8'));if(!x.startedAt)x.startedAt=new Date().toISOString();return x;}
function pnlForExit(p,exit){const gross=(exit-p.entry)*p.qty;const costs=(p.entry*p.qty+exit*p.qty)*(ROUND_TRIP_COST/2);return gross-costs;}
function stats(closed){const wins=closed.filter(t=>t.pnlUSDT>0),losses=closed.filter(t=>t.pnlUSDT<0);const gp=wins.reduce((a,t)=>a+t.pnlUSDT,0),gl=Math.abs(losses.reduce((a,t)=>a+t.pnlUSDT,0));return {trades:closed.length,wins:wins.length,losses:losses.length,winRate:closed.length?wins.length/closed.length:0,profitFactor:gl?gp/gl:(gp>0?999:null),expectancyUSDT:closed.length?closed.reduce((a,t)=>a+t.pnlUSDT,0)/closed.length:0};}

const ledger=loadLedger();
const [tickers,books,info,btcRaw]=await Promise.all([get('/api/v3/ticker/24hr'),get('/api/v3/ticker/bookTicker'),get('/api/v3/exchangeInfo'),candles('BTCUSDT','15m',140)]);
const tm=new Map(tickers.map(x=>[x.symbol,x])), bm=new Map(books.map(x=>[x.symbol,x]));
const btc15=closedOnly(btcRaw);

// Manage existing paper positions first using observable current book prices.
const stillOpen=[];
for(const p of ledger.openPositions){
  const b=bm.get(p.symbol); if(!b){stillOpen.push(p);continue;}
  const bid=+b.bidPrice; let reason=null;
  const ageBars=Math.floor((Date.now()-new Date(p.openedAt).getTime())/(15*60*1000));
  if(bid<=p.stop) reason='STOP';
  else if(bid>=p.target) reason='TARGET';
  else if(ageBars>=p.maxHoldBars) reason='TIME';
  if(reason){
    const pnlUSDT=pnlForExit(p,bid);
    ledger.cashUSDT+=bid*p.qty;
    ledger.realizedPnLUSDT+=pnlUSDT;
    ledger.closedTrades.push({...p,closedAt:new Date().toISOString(),exit:bid,exitReason:reason,pnlUSDT,returnPct:(bid/p.entry-1)*100});
    const d=utcDay(); ledger.daily[d]=(ledger.daily[d]||0)+pnlUSDT;
  } else stillOpen.push(p);
}
ledger.openPositions=stillOpen;

const today=utcDay(); const todayPnL=ledger.daily[today]||0;
const dailyLossGateOpen=todayPnL>-DAILY_LOSS_LIMIT;

// Scan smaller liquid coins. Signals use CLOSED 15m/1h bars only; entry uses current ask after signal formation.
const universe=(info.symbols||[])
 .filter(s=>s.status==='TRADING'&&s.quoteAsset==='USDT'&&s.isSpotTradingAllowed&&supportedBase(s.baseAsset))
 .map(s=>({meta:s,t:tm.get(s.symbol)})).filter(x=>x.t)
 .filter(x=>+x.t.quoteVolume>=5_000_000&&+x.t.quoteVolume<=150_000_000)
 .sort((a,b)=>+b.t.quoteVolume-+a.t.quoteVolume);

let ranked=[];
for(const batch of chunks(universe,6)){
 const rows=await Promise.all(batch.map(async ({meta,t})=>{
  try{
   const [r15,r1h]=await Promise.all([candles(meta.symbol,'15m',260),candles(meta.symbol,'1h',140)]);
   const c15=closedOnly(r15), c1h=closedOnly(r1h); const f=flow(c15), b=bm.get(meta.symbol)||{};
   return scoreCandidate({symbol:meta.symbol,baseAsset:meta.baseAsset,c15,c1h,btc15,quoteVolume24h:+t.quoteVolume,bid:+b.bidPrice,ask:+b.askPrice,...f});
  }catch(e){return {ok:false,symbol:meta.symbol,reason:'DATA_ERROR',error:String(e.message||e),score:0};}
 })); ranked.push(...rows);
}
ranked.sort((a,b)=>(b.score||0)-(a.score||0));

if(dailyLossGateOpen){
 const occupied=new Set(ledger.openPositions.map(p=>p.symbol));
 for(const c of ranked.filter(x=>x.ok)){
  if(ledger.openPositions.length>=MAX_POSITIONS) break;
  if(occupied.has(c.symbol)) continue;
  const available=Math.max(0,ledger.cashUSDT-RESERVE_USDT);
  const notional=Math.min(c.notional,available);
  if(notional<5) continue;
  const qty=notional/c.entry;
  ledger.cashUSDT-=notional;
  ledger.openPositions.push({symbol:c.symbol,strategy:STRATEGY_ID,openedAt:new Date().toISOString(),entry:c.entry,stop:c.stop,target:c.target,qty,notional,maxHoldBars:c.maxHoldBars,score:c.score,metrics:c.metrics});
  occupied.add(c.symbol);
 }
}

const summary={generatedAt:new Date().toISOString(),strategy:STRATEGY_ID,mode:'FORWARD_PAPER_SEMI_LIVE',liveTrading:false,scope:'SMALLER_LIQUID_BINANCE_SPOT_USDT',universeCount:universe.length,dailyLossGateOpen,todayPnLUSDT:todayPnL,cashUSDT:ledger.cashUSDT,realizedPnLUSDT:ledger.realizedPnLUSDT,openPositions:ledger.openPositions,performance:stats(ledger.closedTrades),topCandidates:ranked.slice(0,10)};
fs.mkdirSync('artifacts',{recursive:true});
fs.writeFileSync(LEDGER,JSON.stringify(ledger,null,2)+'\n');
fs.writeFileSync(ART,JSON.stringify(summary,null,2)+'\n');
console.log(JSON.stringify(summary,null,2));
