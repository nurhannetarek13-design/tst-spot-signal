import fs from 'node:fs/promises';
import { scoreCandidate, STRATEGY_ID, supportedBase } from '../src/strategies/small-cap-intraday.mjs';
import { DEFAULT_EXECUTION_POLICY } from '../src/execution/spot-execution-shell.mjs';
import { createFileStateStore } from '../src/execution/file-state-store.mjs';
import { createMockBinanceSpotAdapter } from '../src/execution/mock-binance-spot-adapter.mjs';
import { createTelegramNotifier } from '../src/execution/telegram-notifier.mjs';
import { settlePaperPositions } from '../src/execution/paper-position-manager.mjs';
import { executeRankedPaperCandidates } from '../src/execution/candidate-batch-runtime.mjs';

const BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com','https://data-api.binance.vision'];
const STATE='paper/unified-small-cap-execution-state.json';
const ART='artifacts/forward-paper-small-cap-unified.json';
const policy={...DEFAULT_EXECUTION_POLICY,liveTrading:false,paperMode:true,maxDailyLossUsdt:2,maxOpenPositions:3,maxQuotePerTradeUsdt:10};

async function get(path){let last;for(const b of BASES){try{const r=await fetch(b+path);if(r.ok)return r.json();last=new Error(`${b} ${r.status}`)}catch(e){last=e}}throw last||new Error('NO_MARKET_DATA')}
async function candles(symbol,interval,limit){const x=await get(`/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`);return x.map(k=>({t:+k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4],v:+k[5],q:+k[7],tbq:+k[10],closeTime:+k[6]}));}
const closedOnly=c=>c.filter(x=>x.closeTime<Date.now());
function flow(c){const recent=c.slice(-4),hist=c.slice(-24,-4);const q=recent.reduce((a,x)=>a+x.q,0),tb=recent.reduce((a,x)=>a+x.tbq,0);const avg=hist.reduce((a,x)=>a+x.q,0)/Math.max(1,hist.length);const now=recent.reduce((a,x)=>a+x.q,0)/Math.max(1,recent.length);return {takerBuyRatio:q?tb/q:0,relativeVolume:avg?now/avg:0};}
function chunks(a,n){const out=[];for(let i=0;i<a.length;i+=n)out.push(a.slice(i,i+n));return out;}

const [tickers,books,info,btcRaw]=await Promise.all([get('/api/v3/ticker/24hr'),get('/api/v3/ticker/bookTicker'),get('/api/v3/exchangeInfo'),candles('BTCUSDT','15m',140)]);
const tm=new Map(tickers.map(x=>[x.symbol,x])), bm=new Map(books.map(x=>[x.symbol,x]));
const btc15=closedOnly(btcRaw);
const stateStore=createFileStateStore(STATE);
const notifier=createTelegramNotifier({token:process.env.TELEGRAM_BOT_TOKEN||process.env.TELEGRAM_TOKEN,chatId:process.env.TELEGRAM_CHAT_ID||process.env.TG_CHAT_ID});

const currentPrices={};for(const [symbol,b] of bm)currentPrices[symbol]=+b.bidPrice;
const settled=await settlePaperPositions({stateStore,priceBySymbol:currentPrices,notifier});

const universe=(info.symbols||[])
 .filter(s=>s.status==='TRADING'&&s.quoteAsset==='USDT'&&s.isSpotTradingAllowed&&supportedBase(s.baseAsset))
 .map(s=>({meta:s,t:tm.get(s.symbol)})).filter(x=>x.t)
 .filter(x=>+x.t.quoteVolume>=5_000_000&&+x.t.quoteVolume<=150_000_000)
 .sort((a,b)=>+b.t.quoteVolume-+a.t.quoteVolume);

let ranked=[];
for(const batch of chunks(universe,6)){
  const rows=await Promise.all(batch.map(async({meta,t})=>{try{
    const [r15,r1h]=await Promise.all([candles(meta.symbol,'15m',260),candles(meta.symbol,'1h',140)]);
    const c15=closedOnly(r15),c1h=closedOnly(r1h),f=flow(c15),b=bm.get(meta.symbol)||{};
    return scoreCandidate({symbol:meta.symbol,baseAsset:meta.baseAsset,c15,c1h,btc15,quoteVolume24h:+t.quoteVolume,bid:+b.bidPrice,ask:+b.askPrice,...f});
  }catch(e){return {ok:false,symbol:meta.symbol,reason:'DATA_ERROR',error:String(e.message||e),score:0}}}));
  ranked.push(...rows);
}
ranked.sort((a,b)=>(b.score||0)-(a.score||0));

const fillPrices={};for(const c of ranked.filter(x=>x.ok)){const b=bm.get(c.symbol);if(b)fillPrices[c.symbol]=+b.askPrice;}
const exchange=createMockBinanceSpotAdapter({priceBySymbol:fillPrices});
const batch=await executeRankedPaperCandidates({candidates:ranked,stateStore,exchange,notifier,policy,maxEntriesPerRun:1,minScore:75});
const finalState=await stateStore.load();
const summary={generatedAt:new Date().toISOString(),strategy:STRATEGY_ID,mode:'UNIFIED_FORWARD_PAPER',liveTrading:false,universeCount:universe.length,qualifiedCandidates:ranked.filter(x=>x.ok).length,settled: settled.closed,executed:batch.executed,rejected:batch.rejected,openPositions:finalState.openPositions,realizedPnlTodayUsdt:finalState.realizedPnlTodayUsdt,topCandidates:ranked.slice(0,10),telegramConfigured:notifier.configured};
await fs.mkdir('artifacts',{recursive:true});
await fs.writeFile(ART,JSON.stringify(summary,null,2)+'\n','utf8');
console.log(JSON.stringify(summary,null,2));
