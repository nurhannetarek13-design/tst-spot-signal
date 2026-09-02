import fs from 'node:fs';
import { scoreCandidate, STRATEGY_ID } from '../src/strategies/regime-adaptive-momentum.mjs';

const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return r.json();last=new Error(`${r.status}`);}catch(e){last=e}}throw last||new Error('NO_MARKET_DATA');}
async function candles(symbol,limit=260){const x=await get(`/api/v3/klines?symbol=${symbol}&interval=4h&limit=${limit}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10]}));}
function flow(c){const recent=c.slice(-3);const q=recent.reduce((a,x)=>a+x.q,0);const tb=recent.reduce((a,x)=>a+x.tbq,0);const avg=c.slice(-23,-3).reduce((a,x)=>a+x.q,0)/20;const now=recent.reduce((a,x)=>a+x.q,0)/3;return {takerBuyRatio:q?tb/q:0,relativeVolume:avg?now/avg:0};}
const tickers=await get('/api/v3/ticker/24hr');
const books=await get('/api/v3/ticker/bookTicker');
const bookMap=new Map(books.map(x=>[x.symbol,x]));
const universe=tickers.filter(x=>x.symbol.endsWith('USDT')&&+x.quoteVolume>=20_000_000).sort((a,b)=>+b.quoteVolume-+a.quoteVolume).slice(0,20);
const btc=await candles('BTCUSDT');
let out=[];
for(const t of universe){try{const c=await candles(t.symbol);const f=flow(c);const b=bookMap.get(t.symbol)||{};out.push(scoreCandidate({symbol:t.symbol,candles:c,btcCandles:btc,quoteVolume24h:+t.quoteVolume,bid:+b.bidPrice,ask:+b.askPrice,...f}));}catch(e){out.push({ok:false,symbol:t.symbol,reason:'DATA_ERROR',error:String(e.message||e),strategy:STRATEGY_ID,liveApproved:false});}}
out.sort((a,b)=>(b.score||0)-(a.score||0));
const result={generatedAt:new Date().toISOString(),strategy:STRATEGY_ID,mode:'PAPER_ONLY',liveTrading:false,candidates:out.filter(x=>x.ok),ranked:out};
fs.mkdirSync('artifacts',{recursive:true});fs.writeFileSync('artifacts/paper-regime-adaptive.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
