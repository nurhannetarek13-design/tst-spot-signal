import fs from 'node:fs';
import { scoreCandidate as scoreSmall, supportedBase } from '../src/strategies/small-cap-intraday.mjs';
import { scoreCandidate as scoreLarge } from '../src/strategies/regime-adaptive-momentum.mjs';
const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
const TG_TOKEN=process.env.TELEGRAM_BOT_TOKEN||process.env.TELEGRAM_TOKEN||'';
const TG_CHAT_ID=process.env.TELEGRAM_CHAT_ID||process.env.TG_CHAT_ID||'';
const ART='artifacts/unified-opportunity-scanner.json';
async function get(p){let e;for(const b of BASES){try{const r=await fetch(b+p,{signal:AbortSignal.timeout(20000)});if(r.ok)return r.json();e=new Error(`${b} ${r.status}`)}catch(x){e=x}}throw e||new Error('NO_DATA')}
async function candles(s,i,l){const x=await get(`/api/v3/klines?symbol=${s}&interval=${i}&limit=${l}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10],closeTime:+k[6]})).filter(x=>x.closeTime<Date.now())}
function flow(c){const r=c.slice(-4),h=c.slice(-24,-4),q=r.reduce((a,x)=>a+x.q,0),tb=r.reduce((a,x)=>a+x.tbq,0),av=h.reduce((a,x)=>a+x.q,0)/Math.max(1,h.length),nv=r.reduce((a,x)=>a+x.q,0)/Math.max(1,r.length);return {takerBuyRatio:q?tb/q:0,relativeVolume:av?nv/av:0}}
function chunks(a,n){const o=[];for(let i=0;i<a.length;i+=n)o.push(a.slice(i,i+n));return o}
async function telegram(t){if(!TG_TOKEN||!TG_CHAT_ID)return false;try{const r=await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({chat_id:TG_CHAT_ID,text:t})});return r.ok}catch{return false}}
const [tickers,books,info,btc15,btc4h]=await Promise.all([get('/api/v3/ticker/24hr'),get('/api/v3/ticker/bookTicker'),get('/api/v3/exchangeInfo'),candles('BTCUSDT','15m',260),candles('BTCUSDT','4h',260)]);
const tm=new Map(tickers.map(x=>[x.symbol,x])),bm=new Map(books.map(x=>[x.symbol,x]));
const universe=(info.symbols||[]).filter(s=>s.status==='TRADING'&&s.quoteAsset==='USDT'&&s.isSpotTradingAllowed&&supportedBase(s.baseAsset)).map(s=>({meta:s,t:tm.get(s.symbol),b:bm.get(s.symbol)})).filter(x=>x.t&&x.b&&+x.t.quoteVolume>=5_000_000).sort((a,b)=>+b.t.quoteVolume-+a.t.quoteVolume).slice(0,120);
let ranked=[];
for(const batch of chunks(universe,6)){const rr=await Promise.all(batch.map(async x=>{try{const qv=+x.t.quoteVolume,bid=+x.b.bidPrice,ask=+x.b.askPrice;if(qv<=150_000_000){const [c15,c1h]=await Promise.all([candles(x.meta.symbol,'15m',260),candles(x.meta.symbol,'1h',140)]),f=flow(c15);const z=scoreSmall({symbol:x.meta.symbol,baseAsset:x.meta.baseAsset,c15,c1h,btc15,quoteVolume24h:qv,bid,ask,...f});return {...z,lane:'SMALL_CAP_INTRADAY'}}else{const c4h=await candles(x.meta.symbol,'4h',260),f=flow(c4h);const z=scoreLarge({symbol:x.meta.symbol,baseAsset:x.meta.baseAsset,candles:c4h,btcCandles:btc4h,quoteVolume24h:qv,bid,ask,...f});return {...z,lane:'LARGE_LIQUID_MOMENTUM'}}}catch(e){return {ok:false,symbol:x.meta.symbol,score:0,reason:'DATA_ERROR',error:String(e.message||e)}}}));ranked.push(...rr)}
ranked.sort((a,b)=>(b.ok-a.ok)||(b.score||0)-(a.score||0));const best=ranked.find(x=>x.ok)||null;
const out={generatedAt:new Date().toISOString(),mode:'SIGNALS_ONLY',liveTrading:false,universeCount:universe.length,bestOpportunity:best,qualified:ranked.filter(x=>x.ok).slice(0,5),topRanked:ranked.slice(0,15)};
if(best)await telegram(`🟢 BUY SIGNAL — ${best.symbol}\nLane: ${best.lane}\nScore: ${best.score}/100\nEntry: ${best.entry}\nStop: ${best.stop}\nTarget: ${best.target??'trailing'}\nSize: ${(best.notional||0).toFixed(2)} USDT\nPaper/signal only — no automatic real order.`);
fs.mkdirSync('artifacts',{recursive:true});fs.writeFileSync(ART,JSON.stringify(out,null,2)+'\n');console.log(JSON.stringify(out,null,2));
