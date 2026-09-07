import baseWorker, { SignalState } from "./edge-worker.js";
export { SignalState };

const QUOTE_USDT = 5.5;
const SIGNAL_TTL_SEC = 10 * 60;
const API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function stateStub(env){const id=env.STATE_COORDINATOR.idFromName("global");return env.STATE_COORDINATOR.get(id);}
async function getState(env,key){const r=await stateStub(env).fetch(`https://state/get?key=${encodeURIComponent(key)}`);return r.ok?await r.json():null;}
async function putState(env,key,value,ttlSeconds){const row={value,expiresAt:Date.now()+ttlSeconds*1000};await stateStub(env).fetch(`https://state/put?key=${encodeURIComponent(key)}`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(row)});}

async function tg(env, method, payload){
  if(!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_BOT_TOKEN missing");
  const r=await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});
  const j=await r.json().catch(()=>({ok:false}));
  if(!r.ok||j.ok===false) throw new Error(`Telegram ${method} failed`);
  return j;
}

function fmt(v){return Number(v||0).toLocaleString("en-US",{useGrouping:false,maximumFractionDigits:8});}
function compactId(p){
  const raw=`${p.symbol}:${p.openedAt||p.createdAt||Date.now()}:${p.entry}`;
  let h=2166136261; for(let i=0;i<raw.length;i++){h^=raw.charCodeAt(i);h=Math.imul(h,16777619);} return (h>>>0).toString(36);
}

async function hmacHex(secret, text){
  const key=await crypto.subtle.importKey("raw",new TextEncoder().encode(secret),{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  const sig=await crypto.subtle.sign("HMAC",key,new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map(b=>b.toString(16).padStart(2,"0")).join("");
}

async function signedBinance(env,path,params,method="POST"){
  if(!env.BINANCE_API_KEY||!env.BINANCE_API_SECRET) throw new Error("Binance API secrets missing");
  const baseParams={...params,recvWindow:5000,timestamp:Date.now()};
  const qs=new URLSearchParams(Object.entries(baseParams).map(([k,v])=>[k,String(v)])).toString();
  const signature=await hmacHex(env.BINANCE_API_SECRET,qs);
  let last="unknown";
  for(const base of API_BASES){
    try{
      const r=await fetch(`${base}${path}?${qs}&signature=${signature}`,{method,headers:{"X-MBX-APIKEY":env.BINANCE_API_KEY,"content-type":"application/x-www-form-urlencoded"}});
      const text=await r.text();
      const data=JSON.parse(text||"{}");
      if(!r.ok||data.code<0){last=`${data.code||r.status} ${data.msg||text}`;continue;}
      return data;
    }catch(e){last=String(e?.message||e);}
  }
  throw new Error(`Binance signed request failed: ${last}`);
}

async function publicBinance(path){
  for(const base of ["https://data-api.binance.vision",...API_BASES]){
    try{const r=await fetch(base+path);if(r.ok)return await r.json();}catch{}
  }
  throw new Error("Binance public API unavailable");
}

function floorTo(value,step){if(!step||step<=0)return value;const n=Math.floor((value+1e-12)/step)*step;const d=Math.max(0,Math.ceil(-Math.log10(step)));return Number(n.toFixed(d));}
function roundTo(value,tick){if(!tick||tick<=0)return value;const n=Math.round(value/tick)*tick;const d=Math.max(0,Math.ceil(-Math.log10(tick)));return Number(n.toFixed(d));}

async function executeBuy(env, signal){
  const symbol=signal.symbol;
  const info=await publicBinance(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`);
  const market=info.symbols?.[0];
  if(!market||market.status!=="TRADING"||!market.isSpotTradingAllowed) throw new Error("Pair is not tradable on Spot");
  const lot=market.filters.find(x=>x.filterType==="LOT_SIZE");
  const pf=market.filters.find(x=>x.filterType==="PRICE_FILTER");
  const step=Number(lot?.stepSize||"0.00000001"), tick=Number(pf?.tickSize||"0.00000001");

  const buy=await signedBinance(env,"/api/v3/order",{symbol,side:"BUY",type:"MARKET",quoteOrderQty:QUOTE_USDT,newOrderRespType:"FULL"});
  const executedQty=Number(buy.executedQty||0), quoteQty=Number(buy.cummulativeQuoteQty||0);
  if(executedQty<=0||quoteQty<=0) throw new Error("Market BUY returned zero fill");
  const avg=quoteQty/executedQty;

  const entryRef=Number(signal.entry||avg), stopRef=Number(signal.stop||entryRef*0.98), targetRef=Number(signal.target||entryRef*1.012);
  const stopRatio=stopRef/entryRef, tpRatio=targetRef/entryRef;
  const stop=roundTo(avg*stopRatio,tick), tp=roundTo(avg*tpRatio,tick), stopLimit=roundTo(stop*(1-0.003),tick);
  const sellQty=floorTo(executedQty*0.999,step);
  let oco=null, ocoError=null;
  try{
    oco=await signedBinance(env,"/api/v3/orderList/oco",{symbol,side:"SELL",quantity:sellQty,aboveType:"LIMIT_MAKER",abovePrice:tp,belowType:"STOP_LOSS_LIMIT",belowStopPrice:stop,belowPrice:stopLimit,belowTimeInForce:"GTC"});
  }catch(e){ocoError=String(e?.message||e);}
  return {buy,avg,executedQty,quoteQty,tp,stop,stopLimit,sellQty,oco,ocoError};
}

async function sendPromptForActive(env){
  if(!env.TELEGRAM_BOT_TOKEN||!env.TELEGRAM_CHAT_ID) return;
  const active=(await getState(env,"paper:active"))||[];
  for(const p of active){
    if(!p?.symbol) continue;
    const id=compactId(p), promptKey=`buy-prompt:${id}`;
    if(await getState(env,promptKey)) continue;
    const ageMs=Date.now()-Number(p.openedAt||p.createdAt||Date.now());
    if(ageMs>10*60*1000) continue;
    const signal={id,symbol:p.symbol,entry:Number(p.entry),stop:Number(p.stop),target:Number(p.target),quantity:Number(p.quantity||0),strategy:p.strategy||"NFI/Scanner",score:p.score||null,createdAt:Date.now(),expiresAt:Date.now()+SIGNAL_TTL_SEC*1000};
    await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);
    const baseAsset=p.symbol.endsWith("USDT")?p.symbol.slice(0,-4):p.symbol;
    const url=`https://www.binance.com/en/trade/${encodeURIComponent(baseAsset)}_USDT?type=spot`;
    const text=[
      `🚨 فرصة Binance Spot — ${p.symbol.replace("USDT","/USDT")}`,
      `🧠 ${signal.strategy}${signal.score?` | Score ${signal.score}/100`:""}`,
      `💵 المبلغ عند الضغط: ${QUOTE_USDT} USDT`,
      `💲 Entry تقريبي: ${fmt(signal.entry)}`,
      `🛑 SL: ${fmt(signal.stop)}`,
      `🎯 TP: ${fmt(signal.target)}`,
      `⏱️ صالحة ${Math.round(SIGNAL_TTL_SEC/60)} دقائق`,
      "",
      "لن يتم شراء أي شيء إلا بعد ضغطك على BUY."
    ].join("\n");
    await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text,disable_web_page_preview:true,reply_markup:{inline_keyboard:[[{text:`✅ BUY ${QUOTE_USDT} USDT`,callback_data:`BUY:${id}`}],[{text:"📈 افتح Binance Spot",url}]]}});
    await putState(env,promptKey,{sentAt:Date.now()},SIGNAL_TTL_SEC);
  }
}

async function handleTelegramWebhook(request,env){
  const update=await request.json().catch(()=>null);
  const q=update?.callback_query;
  if(!q) return new Response("ok");
  const chatId=String(q.message?.chat?.id||"");
  if(chatId!==String(env.TELEGRAM_CHAT_ID||"")){
    await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"غير مسموح",show_alert:true});
    return new Response("ok");
  }
  const data=String(q.data||"");
  if(!data.startsWith("BUY:")) return new Response("ok");
  const id=data.slice(4), key=`live-signal:${id}`;
  const signal=await getState(env,key);
  if(!signal||Date.now()>Number(signal.expiresAt||0)){
    await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"الفرصة انتهت صلاحيتها",show_alert:true});
    return new Response("ok");
  }
  const used=await getState(env,`live-used:${id}`);
  if(used){await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"تم تنفيذ الزر قبل كده",show_alert:true});return new Response("ok");}
  await putState(env,`live-used:${id}`,{startedAt:Date.now()},24*3600);
  await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"جاري تنفيذ BUY على Binance Spot…"});
  try{
    const r=await executeBuy(env,signal);
    const protection=r.oco?`✅ OCO اتفعل: TP ${fmt(r.tp)} | SL ${fmt(r.stop)}`:`⚠️ الشراء تم لكن OCO فشل: ${r.ocoError}`;
    await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:[`✅ BUY تم — ${signal.symbol.replace("USDT","/USDT")}`,`💵 المصروف: ${fmt(r.quoteQty)} USDT`,`📦 الكمية: ${fmt(r.executedQty)}`,`💲 متوسط التنفيذ: ${fmt(r.avg)}`,protection].join("\n")});
    return new Response("ok");
  }catch(e){
    await putState(env,`live-used:${id}`,{failedAt:Date.now(),error:String(e?.message||e)},300);
    await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:`❌ BUY فشل — ${signal.symbol}\n${String(e?.message||e).slice(0,350)}`});
    return new Response("ok");
  }
}

export default {
  async fetch(request,env,ctx){
    const url=new URL(request.url);
    if(url.pathname==="/telegram-webhook"&&request.method==="POST") return handleTelegramWebhook(request,env);
    if(url.pathname==="/buy-gateway-status") return Response.json({ok:true,mode:"USER_CONFIRMATION_ONLY",autoBuy:false,quoteUSDT:QUOTE_USDT,telegramConfigured:Boolean(env.TELEGRAM_BOT_TOKEN&&env.TELEGRAM_CHAT_ID),binanceConfigured:Boolean(env.BINANCE_API_KEY&&env.BINANCE_API_SECRET)});
    return baseWorker.fetch(request,env,ctx);
  },
  async scheduled(event,env,ctx){
    ctx.waitUntil((async()=>{
      if(baseWorker.scheduled) await baseWorker.scheduled(event,env,ctx);
      await sendPromptForActive(env);
    })());
  }
};
