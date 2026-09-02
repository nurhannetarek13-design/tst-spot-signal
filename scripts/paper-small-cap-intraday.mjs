import fs from 'node:fs';
import {scoreCandidate,STRATEGY_ID,supportedBase} from '../src/strategies/small-cap-intraday.mjs';

const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return r.json();last=new Error(`${b} ${r.status}`)}catch(e){last=e}}throw last||new Error('NO_MARKET_DATA')}
async function candles(symbol,interval,limit){const x=await get(`/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10]}));}
function flow(c){const recent=c.slice(-4);const q=recent.reduce((a,x)=>a+x.q,0),tb=recent.reduce((a,x)=>a+x.tbq,0);const hist=c.slice(-24,-4);const avg=hist.reduce((a,x)=>a+x.q,0)/Math.max(1,hist.length);const now=recent.reduce((a,x)=>a+x.q,0)/recent.length;return {takerBuyRatio:q?tb/q:0,relativeVolume:avg?now/avg:0};}
function chunks(a,n){const o=[];for(let i=0;i<a.length;i+=n)o.push(a.slice(i,i+n));return o;}

const [tickers,books,info,btc15]=await Promise.all([get('/api/v3/ticker/24hr'),get('/api/v3/ticker/bookTicker'),get('/api/v3/exchangeInfo'),candles('BTCUSDT','15m',120)]);
const tm=new Map(tickers.map(x=>[x.symbol,x])),bm=new Map(books.map(x=>[x.symbol,x]));
const universe=(info.symbols||[])
 .filter(s=>s.status==='TRADING'&&s.quoteAsset==='USDT'&&s.isSpotTradingAllowed&&supportedBase(s.baseAsset))
 .map(s=>({meta:s,t:tm.get(s.symbol)})).filter(x=>x.t)
 .filter(x=>+x.t.quoteVolume>=5_000_000&&+x.t.quoteVolume<=150_000_000)
 .sort((a,b)=>+b.t.quoteVolume-+a.t.quoteVolume);

let out=[];
for(const batch of chunks(universe,6)){
 const rows=await Promise.all(batch.map(async ({meta,t})=>{
   try{
    const [c15,c1h]=await Promise.all([candles(meta.symbol,'15m',240),candles(meta.symbol,'1h',120)]);
    const f=flow(c15),b=bm.get(meta.symbol)||{};
    return scoreCandidate({symbol:meta.symbol,baseAsset:meta.baseAsset,c15,c1h,btc15,quoteVolume24h:+t.quoteVolume,bid:+b.bidPrice,ask:+b.askPrice,...f});
   }catch(e){return {ok:false,symbol:meta.symbol,reason:'DATA_ERROR',error:String(e.message||e),strategy:STRATEGY_ID,liveApproved:false};}
 })); out.push(...rows);
}
out.sort((a,b)=>(b.score||0)-(a.score||0));
const result={generatedAt:new Date().toISOString(),strategy:STRATEGY_ID,mode:'PAPER_ONLY',liveTrading:false,scope:'SMALLER_LIQUID_BINANCE_SPOT_USDT',universeCount:universe.length,candidates:out.filter(x=>x.ok).slice(0,5),ranked:out.slice(0,30)};
fs.mkdirSync('artifacts',{recursive:true});fs.writeFileSync('artifacts/paper-small-cap-intraday.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result,null,2));
