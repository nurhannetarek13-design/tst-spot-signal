import http from "node:http";
import { pathToFileURL } from "node:url";

const PORT=Number(process.env.PORT||8080);
const MAX_SYMBOLS=Math.max(3,Math.min(30,Number(process.env.STREAM_MAX_SYMBOLS||25)));
const MIN_QV=Number(process.env.STREAM_MIN_QUOTE_VOLUME_USDT||20000000);
const PUBLIC_RELAY=String(process.env.PUBLIC_MARKET_RELAY_URL||"").replace(/\/$/,"");
const REST_BASES=[
  "https://data-api.binance.vision",
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];
const WS=String(process.env.PUBLIC_WS_BASE||"wss://data-stream.binance.vision:443/stream?streams=");
const STABLES=new Set(["USDC","FDUSD","TUSD","USDP","DAI","BUSD","USD1","RLUSD","USDE","EUR","AEUR","TRY","BRL","GBP","AUD"]);
const LEV=["UP","DOWN","BULL","BEAR"];

const runtime={
  mode:"SHADOW_ONLY",liveTrading:false,connected:false,ws:null,lastMessageAt:null,
  reconnects:0,resyncs:0,lastError:null,clock:{ok:false},universe:[],books:new Map()
};
const now=()=>Date.now();
async function get(path){
  const targets=[];
  if(PUBLIC_RELAY) targets.push(PUBLIC_RELAY+"?path="+encodeURIComponent(path));
  for(const base of REST_BASES) targets.push(base+path);
  let last="unavailable";
  for(const url of targets){
    try{
      const r=await fetch(url,{headers:{"cache-control":"no-store","accept":"application/json"},signal:AbortSignal.timeout(10000)});
      if(r.ok)return await r.json();
      last="HTTP_"+r.status+":"+url;
    }catch(e){last=String(e?.message||e);}
  }
  throw new Error("PUBLIC_REST_UNAVAILABLE:"+path+":"+last);
}
function eligible(s){
  const b=String(s.baseAsset||"");
  return s.status==="TRADING"&&s.quoteAsset==="USDT"&&s.isSpotTradingAllowed===true&&b&&!STABLES.has(b)&&!LEV.some(x=>b.endsWith(x));
}
export function sequenceStatus(last,event){
  const U=Number(event.U),u=Number(event.u);
  if(!Number.isFinite(U)||!Number.isFinite(u))return "INVALID";
  if(u<=Number(last))return "OLD";
  if(U>Number(last)+1)return "GAP";
  return "APPLY";
}
export function applySide(map,rows,flow,ts,side){
  for(const [p0,q0] of rows||[]){
    const p=Number(p0),q=Number(q0);if(!(p>0)||!Number.isFinite(q))continue;
    const prev=Number(map.get(p)||0); if(q<=0)map.delete(p);else map.set(p,q);
    const d=q-prev;
    if(Math.abs(d)>1e-15)flow.push({ts,side,type:d>0?"add":"cancel",quote:Math.abs(d)*p});
  }
  while(flow.length&&ts-flow[0].ts>60000)flow.shift();
}
function stateFor(symbol){
  return {symbol,bids:new Map(),asks:new Map(),flow:[],trades:[],buffer:[],synced:false,syncing:false,lastUpdateId:0,lastDepth:null,lastTrade:null,warmSince:null,gaps:0};
}
function top(map,desc,n=5){return [...map.entries()].sort((a,b)=>desc?b[0]-a[0]:a[0]-b[0]).slice(0,n);}
export function bookMetrics(s,t=Date.now()){
  const bids=top(s.bids,true),asks=top(s.asks,false);
  const bq=bids.reduce((z,[p,q])=>z+p*q,0),aq=asks.reduce((z,[p,q])=>z+p*q,0),den=bq+aq;
  const bid=bids[0]?.[0]??null,ask=asks[0]?.[0]??null,bqty=bids[0]?.[1]??0,aqty=asks[0]?.[1]??0;
  const mid=bid&&ask?(bid+ask)/2:null;
  const micro=bid&&ask&&bqty+aqty>0?(ask*bqty+bid*aqty)/(bqty+aqty):null;
  const f=s.flow.filter(x=>t-x.ts<=10000);
  const adds=f.filter(x=>x.type==="add").reduce((z,x)=>z+x.quote,0);
  const cancels=f.filter(x=>x.type==="cancel").reduce((z,x)=>z+x.quote,0);
  return {
    bestBid:bid,bestAsk:ask,spreadBps:mid?((ask-bid)/mid)*10000:null,
    bidLiquidityQuote5:bq,askLiquidityQuote5:aq,
    obi:den>0?bq/den:null,microprice:micro,micropriceBiasBps:mid&&micro?((micro-mid)/mid)*10000:null,
    cancellationRate10s:adds+cancels>0?cancels/(adds+cancels):null,
    bidCancelQuote10s:f.filter(x=>x.type==="cancel"&&x.side==="bid").reduce((z,x)=>z+x.quote,0),
    askCancelQuote10s:f.filter(x=>x.type==="cancel"&&x.side==="ask").reduce((z,x)=>z+x.quote,0)
  };
}
export function tradeMetrics(trades,t=Date.now()){
  const r=trades.filter(x=>t-x.ts<=60000);
  const buy=r.filter(x=>x.d>0).reduce((z,x)=>z+x.d,0),sell=Math.abs(r.filter(x=>x.d<0).reduce((z,x)=>z+x.d,0));
  const total=buy+sell;
  const buckets=new Map();
  for(const x of r){
    const k=Math.floor(x.ts/10000);
    buckets.set(k,(buckets.get(k)||0)+x.d);
  }
  const vals=[...buckets.entries()].sort((a,b)=>a[0]-b[0]).map(x=>x[1]);
  return {
    deltaQuote60s:buy-sell,
    takerBuyRatio60s:total>0?buy/total:null,
    tradeEvents60s:r.length,
    cvdSlopePositive10s:vals.length>=2 ? (vals.at(-1)>vals[0] && vals.reduce((a,b)=>a+b,0)>0) : false,
  };
}
async function universe(){
  const [info,tick]=await Promise.all([get("/api/v3/exchangeInfo"),get("/api/v3/ticker/24hr")]);
  const tm=new Map((tick||[]).map(x=>[x.symbol,x]));
  const rows=(info.symbols||[]).filter(eligible).map(s=>({s:s.symbol,q:Number(tm.get(s.symbol)?.quoteVolume||0)}))
    .filter(x=>x.q>=MIN_QV).sort((a,b)=>b.q-a.q).slice(0,MAX_SYMBOLS).map(x=>x.s);
  if(!rows.includes("BTCUSDT"))rows.unshift("BTCUSDT");
  return [...new Set(rows)].slice(0,MAX_SYMBOLS);
}
async function sync(symbol){
  const s=runtime.books.get(symbol);if(!s||s.syncing)return;s.syncing=true;
  let retry=false;
  try{
    const snap=await get("/api/v3/depth?symbol="+encodeURIComponent(symbol)+"&limit=1000");
    const nextBids=new Map(),nextAsks=new Map(),nextFlow=[];
    for(const [p,q] of snap.bids||[])if(Number(q)>0)nextBids.set(Number(p),Number(q));
    for(const [p,q] of snap.asks||[])if(Number(q)>0)nextAsks.set(Number(p),Number(q));
    let id=Number(snap.lastUpdateId||0);

    // Preserve native WebSocket arrival order. Discard only events already
    // covered by the REST snapshot; the first newer event must bridge id+1.
    const pending=s.buffer.filter(e=>Number(e.u)>id);
    let bridged=pending.length===0;
    for(const e of pending){
      const U=Number(e.U),u=Number(e.u);
      if(!bridged){
        if(!(U<=id+1&&id+1<=u)){
          if(U>id+1) throw new Error("SNAPSHOT_BRIDGE_GAP");
          continue;
        }
        bridged=true;
      }else if(U>id+1){
        throw new Error("SYNC_GAP");
      }
      if(u<=id)continue;
      const ts=Number(e.E||now());
      applySide(nextBids,e.b,nextFlow,ts,"bid");
      applySide(nextAsks,e.a,nextFlow,ts,"ask");
      id=u;
      s.lastDepth=ts;
    }
    if(pending.length>0&&!bridged)throw new Error("SNAPSHOT_BRIDGE_NOT_FOUND");

    s.bids=nextBids;s.asks=nextAsks;s.flow=nextFlow;
    s.lastUpdateId=id;s.synced=true;s.warmSince=now();
    s.buffer=s.buffer.filter(e=>Number(e.u)>id);
    runtime.resyncs++;
  }catch(e){
    runtime.lastError="SYNC:"+symbol+":"+String(e?.message||e);
    s.synced=false;
    retry=true;
  }finally{
    s.syncing=false;
    if(retry&&runtime.connected)setTimeout(()=>void sync(symbol),300);
  }
}
function depth(symbol,d){
  const s=runtime.books.get(symbol);if(!s)return;
  if(!s.synced){s.buffer.push(d);if(s.buffer.length>5000)s.buffer.splice(0,s.buffer.length-5000);return;}
  const st=sequenceStatus(s.lastUpdateId,d);if(st==="OLD")return;
  if(st!=="APPLY"){s.synced=false;s.gaps++;runtime.resyncs++;void sync(symbol);return;}
  const ts=Number(d.E||now());applySide(s.bids,d.b,s.flow,ts,"bid");applySide(s.asks,d.a,s.flow,ts,"ask");s.lastUpdateId=Number(d.u);s.lastDepth=ts;
}
function trade(symbol,d){
  const s=runtime.books.get(symbol);if(!s)return;const ts=Number(d.T||d.E||now()),q=Number(d.p||0)*Number(d.q||0);
  if(!(q>=0))return;s.trades.push({ts,d:d.m?-q:q});while(s.trades.length&&ts-s.trades[0].ts>180000)s.trades.shift();s.lastTrade=ts;
}
function snapshot(symbol){
  const s=runtime.books.get(symbol);if(!s)return null;const t=now(),da=s.lastDepth==null?Infinity:t-s.lastDepth,ta=s.lastTrade==null?Infinity:t-s.lastTrade;
  return {symbol,synced:s.synced,warmed:s.synced&&s.warmSince!=null&&t-s.warmSince>=3000,fresh:s.synced&&da<=3000&&ta<=12000,
    depthAgeMs:Number.isFinite(da)?da:null,tradeAgeMs:Number.isFinite(ta)?ta:null,lastUpdateId:s.lastUpdateId,sequenceGaps:s.gaps,
    ...bookMetrics(s,t),...tradeMetrics(s.trades,t)};
}
function health(){
  const rows=runtime.universe.map(snapshot).filter(Boolean),good=rows.filter(x=>x.fresh&&x.warmed).length,ratio=rows.length?good/rows.length:0;
  const clockFresh=runtime.clock.checkedAt&&now()-runtime.clock.checkedAt<120000&&runtime.clock.ok;
  return {ok:runtime.connected&&ratio>=0.8&&clockFresh,mode:"SHADOW_ONLY",liveTrading:false,connected:runtime.connected,
    universeCount:rows.length,freshWarmCount:good,freshWarmRatio:ratio,clock:runtime.clock,reconnects:runtime.reconnects,resyncs:runtime.resyncs,
    lastMessageAgeMs:runtime.lastMessageAt?now()-runtime.lastMessageAt:null,lastError:runtime.lastError};
}
async function clock(){
  const a=now(),r=await get("/api/v3/time"),b=now(),rtt=b-a,offset=Number(r.serverTime||0)-(a+rtt/2);
  runtime.clock={ok:Math.abs(offset)<=750&&rtt<=1500,offsetMs:offset,rttMs:rtt,checkedAt:now()};
}
function streamNames(symbols){
  const out=[];for(const s of symbols){const x=s.toLowerCase();out.push(x+"@depth@100ms",x+"@aggTrade");}return out;
}
async function connect(){
  const u=await universe();runtime.universe=u;runtime.books=new Map(u.map(s=>[s,stateFor(s)]));
  const ws=new WebSocket(WS+streamNames(u).join("/"));runtime.ws=ws;
  ws.addEventListener("open",()=>{runtime.connected=true;runtime.reconnects++;for(const s of u)void sync(s);});
  ws.addEventListener("message",async ev=>{runtime.lastMessageAt=now();try{const m=JSON.parse(typeof ev.data==="string"?ev.data:await ev.data.text()),d=m.data||{},st=String(m.stream||""),sym=String(d.s||st.split("@")[0]).toUpperCase();if(st.includes("@depth"))depth(sym,d);else if(st.includes("@aggTrade"))trade(sym,d);}catch(e){runtime.lastError="PARSE:"+String(e?.message||e);}});
  let closing=false;const restart=()=>{if(closing)return;closing=true;runtime.connected=false;setTimeout(()=>void connect().catch(e=>runtime.lastError=String(e)),1500);};
  ws.addEventListener("error",restart);ws.addEventListener("close",restart);setTimeout(()=>{try{ws.close();}catch{}},23*60*60*1000);
}
function send(res,status,obj){const raw=JSON.stringify(obj);res.writeHead(status,{"content-type":"application/json","cache-control":"no-store"});res.end(raw);}
const server=http.createServer((req,res)=>{const u=new URL(req.url||"/","http://"+(req.headers.host||"localhost"));if(u.pathname==="/health"){const h=health();return send(res,h.ok?200:503,h);}if(u.pathname==="/snapshot"){const sym=String(u.searchParams.get("symbol")||"").toUpperCase();return send(res,200,{ok:true,health:health(),snapshot:sym?snapshot(sym):null,snapshots:sym?undefined:runtime.universe.map(snapshot).filter(Boolean)});}return send(res,200,{ok:true,service:"tst-spot-stream-shadow",mode:"SHADOW_ONLY",liveTrading:false});});
export async function start(){await clock().catch(e=>runtime.lastError=String(e));setInterval(()=>void clock().catch(e=>runtime.lastError=String(e)),60000);setInterval(()=>{if(runtime.connected&&runtime.lastMessageAt&&now()-runtime.lastMessageAt>5000){runtime.lastError="STREAM_STALE_RECONNECT";try{runtime.ws?.close();}catch{}}},1000);server.listen(PORT,"0.0.0.0");await connect();}
if(import.meta.url===pathToFileURL(process.argv[1]||"").href)start().catch(e=>{console.error("[stream-shadow]",String(e?.message||e));process.exit(1);});
