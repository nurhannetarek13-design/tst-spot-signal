import fs from 'node:fs';
import { scoreCandidate as scoreLarge } from '../src/strategies/regime-adaptive-momentum.mjs';
import { scoreCandidate as scoreSmall, supportedBase } from '../src/strategies/small-cap-intraday.mjs';

const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
const TG_TOKEN=process.env.TELEGRAM_BOT_TOKEN||process.env.TELEGRAM_TOKEN||'';
const TG_CHAT_ID=process.env.TELEGRAM_CHAT_ID||process.env.TG_CHAT_ID||'';
const OUT='artifacts/unified-opportunity-scanner.json';
const MAX_SIGNALS=2;
async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return r.json();last=new Error(`${b} ${r.status}`)}catch(e){last=e}}throw last||new Error('NO_MARKET_DATA')}
async function candles(symbol,interval,limit){const x=await get(`/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10],closeTime:+k[6]})).filter(x=>x.closeTime<Date.now());}
function flow(c){const recent=c.slice(-4),hist=c.slice(-24,-4);const q=recent.reduce((a,x)=>a+x.q,0),tb=recent.reduce((a,x)=>a+x.tbq,0),ra=recent.reduce((a,x)=>a+x.q,0)/Math.max(1,recent.length),ha=hist.reduce((a,x)=>a+x.q,0)/Math.max(1,hist.length);return {takerBuyRatio:q?tb/q:0,relativeVolume:ha?ra/ha:0};}
function chunks(a,n){const o=[];for(let i=0;i<a.length;i+=n)o.push(a.slice(i,i+n));return o;}
async function telegram(text){if(!TG_TOKEN||!TG_CHAT_ID)return false;try{const r=await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({chat_id:TG_CHAT_ID,text})});return r.ok}catch{return false}}
const [tickers,books,info,btc4h,btc15]=await Promise.all([get('/api/v3/ticker/24hr'),get('/api/v3/ticker/bookTicker'),get('/api/v3/exchangeInfo'),candles('BTCUSDT','4h',260),candles('BTCUSDT','15m',160)]);
const tm=new Map(tickers.map(x=>[x.symbol,x])),bm=new Map(books.map(x=>[x.symbol,x]));
const universe=(info.symbols||[]).filter(s=>s.status==='TRADING'&&s.quoteAsset==='USDT'&&s.isSpotTradingAllowed&&supportedBase(s.baseAsset)&&tm.has(s.symbol)).map(s=>({meta:s,t:tm.get(s.symbol)})).filter(x=>+x.t.quoteVolume>=5_000_000).sort((a,b)=>+b.t.quoteVolume-+a.t.quoteVolume);
let ranked=[];
for(const batch of chunks(universe,5)){
 const rows=await Promise.all(batch.map(async ({meta,t})=>{try{
  const book=bm.get(meta.symbol)||{},qv=+t.quoteVolume;
  if(qv<=150_000_000){const [c15,c1h]=await Promise.all([candles(meta.symbol,'15m',260),candles(meta.symbol,'1h',140)]);const f=flow(c15);const x=scoreSmall({symbol:meta.symbol,baseAsset:meta.baseAsset,c15,c1h,btc15,quoteVolume24h:qv,bid:+book.bidPrice,ask:+book.askPrice,...f});return {...x,lane:'SMALL_CAP_INTRADAY',target:x.target??null};}
  const c4=await candles(meta.symbol,'4h',260);const f=flow(c4);const x=scoreLarge({symbol:meta.symbol,candles:c4,btcCandles:btc4h,quoteVolume24h:qv,bid:+book.bidPrice,ask:+book.askPrice,...f});return {...x,lane:'LARGE_LIQUID_MOMENTUM',target:null};
 }catch(e){return {ok:false,symbol:meta.symbol,score:0,reason:'DATA_ERROR',error:String(e.message||e)}}}));ranked.push(...rows);
}
ranked.sort((a,b)=>(b.ok-a.ok)||((b.score||0)-(a.score||0)));
const signals=ranked.filter(x=>x.ok).slice(0,MAX_SIGNALS);
for(const s of signals){await telegram(`🟢 BUY WATCH — ${s.symbol}\nType: ${s.lane}\nScore: ${s.score}/100\nEntry: ${s.entry}\nStop: ${s.stop}${s.target?`\nTarget: ${s.target}`:''}\nSize: ${(s.notional||0).toFixed(2)} USDT\nPaper signal — not guaranteed profit.`)}
const out={generatedAt:new Date().toISOString(),mode:'UNIFIED_PAPER_OPPORTUNITY_SCANNER',liveTrading:false,universeCount:universe.length,qualifiedSignals:signals,topCandidates:ranked.slice(0,15),telegramConfigured:Boolean(TG_TOKEN&&TG_CHAT_ID)};
fs.mkdirSync('artifacts',{recursive:true});fs.writeFileSync(OUT,JSON.stringify(out,null,2)+'\n');console.log(JSON.stringify(out,null,2));
