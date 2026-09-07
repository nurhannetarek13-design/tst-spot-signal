import baseWorker, { SignalState } from "./edge-worker.js";
export { SignalState };

const SIGNAL_TTL_SEC = 10 * 60;
const BALANCE_CHECK_SEC = 5 * 60;
const MIN_ORDER_USDT = 5.0;
const MAX_BALANCE_FRACTION = 0.80;
const MAX_RISK_USDT = 0.20;
const BINANCE_API_BASES = [
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

async function tg(env,method,payload){
  if(!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_BOT_TOKEN missing");
  const r=await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});
  const j=await r.json().catch(()=>({ok:false}));
  if(!r.ok||j.ok===false) throw new Error(`Telegram ${method} failed`);
  return j;
}

function fmt(v){return Number(v||0).toLocaleString("en-US",{useGrouping:false,maximumFractionDigits:8});}
function compactId(p){const raw=`${p.symbol}:${p.openedAt||p.createdAt||Date.now()}:${p.entry}`;let h=2166136261;for(let i=0;i<raw.length;i++){h^=raw.charCodeAt(i);h=Math.imul(h,16777619);}return(h>>>0).toString(36);}
async function hmacHex(secret,text){const key=await crypto.subtle.importKey("raw",new TextEncoder().encode(secret),{name:"HMAC",hash:"SHA-256"},false,["sign"]);const sig=await crypto.subtle.sign("HMAC",key,new TextEncoder().encode(text));return[...new Uint8Array(sig)].map(b=>b.toString(16).padStart(2,"0")).join("");}
function sanitizeBinanceError(text,status){try{const j=JSON.parse(text||"{}");return `${j.code||status}: ${j.msg||"Binance request failed"}`;}catch{return `HTTP ${status}`;}}

async function publicBinance(path){
  let last="BINANCE_PUBLIC_UNAVAILABLE";
  for(const base of ["https://data-api.binance.vision",...BINANCE_API_BASES]){
    try{const r=await fetch(base+path),text=await r.text();if(r.ok)return JSON.parse(text||"{}");last=sanitizeBinanceError(text,r.status);}catch(e){last=String(e?.message||e);}
  }
  throw new Error(last);
}

async function signedBinanceDirect(env,method,path,params={}){
  if(!env.BINANCE_API_KEY||!env.BINANCE_API_SECRET) throw new Error("BINANCE_KEYS_MISSING_IN_CLOUDFLARE");
  const all={...params,recvWindow:5000,timestamp:Date.now()};
  const qs=new URLSearchParams(Object.entries(all).map(([k,v])=>[k,String(v)])).toString();
  const signature=await hmacHex(env.BINANCE_API_SECRET,qs);
  let last="BINANCE_UNAVAILABLE";
  for(const base of BINANCE_API_BASES){
    try{
      const r=await fetch(`${base}${path}?${qs}&signature=${signature}`,{method,headers:{"X-MBX-APIKEY":env.BINANCE_API_KEY,"content-type":"application/x-www-form-urlencoded"}});
      const text=await r.text();
      if(r.ok){const data=JSON.parse(text||"{}");if(!(Number(data.code)<0))return data;}
      last=sanitizeBinanceError(text,r.status);
    }catch(e){last=String(e?.message||e);}
  }
  throw new Error(last);
}

function decimals(step){const s=String(step);if(s.includes("e-"))return Number(s.split("e-")[1]);return(s.split(".")[1]||"").replace(/0+$/,"").length;}
function floorTo(v,s){return!s||s<=0?v:Number((Math.floor((v+1e-12)/s)*s).toFixed(decimals(s)));}
function roundTo(v,t){return!t||t<=0?v:Number((Math.round(v/t)*t).toFixed(decimals(t)));}
function dynamicQuote(freeUSDT,entry,stop){const stopPct=(entry-stop)/entry;if(!(stopPct>0))throw new Error("INVALID_STOP_DISTANCE");const byRisk=MAX_RISK_USDT/stopPct;const byBalance=freeUSDT*MAX_BALANCE_FRACTION;return Math.floor(Math.min(byRisk,byBalance)*100)/100;}

async function getBalanceDirect(env){
  const account=await signedBinanceDirect(env,"GET","/api/v3/account",{});
  const get=(asset)=>{const b=(account.balances||[]).find(x=>x.asset===asset)||{};const free=Number(b.free||0),locked=Number(b.locked||0);return{free,locked,total:free+locked};};
  return {ok:true,status:"ACCOUNT_BALANCE_OK",canTrade:Boolean(account.canTrade),usdt:get("USDT"),sol:get("SOL"),source:"CLOUDFLARE_DIRECT_BINANCE",at:Date.now()};
}

async function refreshBalance(env){
  try{const balance=await getBalanceDirect(env);await putState(env,"binance:balance:last",balance,2*3600);await putState(env,"binance:balance:error",null,60);return balance;}
  catch(e){const error={at:Date.now(),error:String(e?.message||e)};await putState(env,"binance:balance:error",error,1800);return null;}
}

async function executeDirect(env,signal){
  const entryRef=Number(signal.entry),stopRef=Number(signal.stop),targetRef=Number(signal.target),symbol=String(signal.symbol||"").toUpperCase();
  if(!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) throw new Error("BAD_SYMBOL");
  if(![entryRef,stopRef,targetRef].every(Number.isFinite)||!(stopRef<entryRef&&targetRef>entryRef)) throw new Error("INVALID_TP_SL_GEOMETRY");
  const info=await publicBinance(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`),market=info.symbols?.[0];
  if(!market||market.status!=="TRADING"||!market.isSpotTradingAllowed||market.quoteAsset!=="USDT") throw new Error("PAIR_NOT_TRADABLE_SPOT");
  const account=await signedBinanceDirect(env,"GET","/api/v3/account",{});
  if(!account.canTrade) throw new Error("ACCOUNT_CANNOT_TRADE");
  const freeUSDT=Number((account.balances||[]).find(b=>b.asset==="USDT")?.free||0);
  const quoteUSDT=dynamicQuote(freeUSDT,entryRef,stopRef);
  if(quoteUSDT<MIN_ORDER_USDT) throw new Error(`SIZE_BELOW_MINIMUM:${quoteUSDT}`);

  const buy=await signedBinanceDirect(env,"POST","/api/v3/order",{symbol,side:"BUY",type:"MARKET",quoteOrderQty:quoteUSDT.toFixed(2),newOrderRespType:"FULL",newClientOrderId:`TSTU${crypto.randomUUID().replaceAll("-","").slice(0,12)}`});
  const executedQty=Number(buy.executedQty||0),spent=Number(buy.cummulativeQuoteQty||0);
  if(!(executedQty>0&&spent>0)) throw new Error("BUY_ZERO_FILL");
  const avg=spent/executedQty,lot=market.filters.find(x=>x.filterType==="LOT_SIZE"),pf=market.filters.find(x=>x.filterType==="PRICE_FILTER"),step=Number(lot?.stepSize||"0.00000001"),tick=Number(pf?.tickSize||"0.00000001");
  const stop=roundTo(avg*(stopRef/entryRef),tick),tp=roundTo(avg*(targetRef/entryRef),tick),stopLimit=roundTo(stop*0.997,tick),sellQty=floorTo(executedQty*0.999,step);
  let oco=null,ocoError=null;
  try{oco=await signedBinanceDirect(env,"POST","/api/v3/orderList/oco",{symbol,side:"SELL",quantity:sellQty,aboveType:"LIMIT_MAKER",abovePrice:tp,belowType:"STOP_LOSS_LIMIT",belowStopPrice:stop,belowPrice:stopLimit,belowTimeInForce:"GTC"});}
  catch(e){ocoError=String(e?.message||e);}
  return {ok:true,status:oco?"BOUGHT_AND_PROTECTED":"BOUGHT_PROTECTION_FAILED",symbol,freeUSDTBefore:freeUSDT,recommendedUSDT:quoteUSDT,quoteUSDT:spent,plannedRiskUSDT:Math.min(MAX_RISK_USDT,quoteUSDT*((entryRef-stopRef)/entryRef)),executedQty,avg,tp,stop,stopLimit,ocoPlaced:Boolean(oco),ocoError,autoBuy:false,userConfirmed:true,sizing:"RISK_BASED_DYNAMIC",executionBackend:"CLOUDFLARE_DIRECT_BINANCE"};
}

async function sendPromptForActive(env){
  if(!env.TELEGRAM_BOT_TOKEN||!env.TELEGRAM_CHAT_ID) return;
  const active=(await getState(env,"paper:active"))||[];
  const balance=(await getState(env,"binance:balance:last"))||await refreshBalance(env);
  const freeUSDT=Number(balance?.usdt?.free||0);
  for(const p of active){
    if(!p?.symbol) continue;
    const id=compactId(p),promptKey=`buy-prompt:${id}`;if(await getState(env,promptKey)) continue;
    const sourceTime=Number(p.openedAt||p.createdAt||Date.now());if(Date.now()-sourceTime>10*60*1000) continue;
    const signal={id,symbol:p.symbol,entry:Number(p.entry),stop:Number(p.stop),target:Number(p.target),strategy:p.strategy||"NFI/Scanner",score:p.score||null,createdAt:Date.now(),expiresAt:Date.now()+SIGNAL_TTL_SEC*1000};
    if(!(signal.entry>0&&signal.stop>0&&signal.target>0&&signal.stop<signal.entry&&signal.target>signal.entry)) continue;
    const recommended=dynamicQuote(freeUSDT,signal.entry,signal.stop),canBuy=recommended>=MIN_ORDER_USDT;
    signal.recommendedUSDT=recommended;await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);
    const baseAsset=p.symbol.endsWith("USDT")?p.symbol.slice(0,-4):p.symbol,url=`https://www.binance.com/en/trade/${encodeURIComponent(baseAsset)}_USDT?type=spot`;
    const riskPct=((signal.entry-signal.stop)/signal.entry)*100,profitPct=((signal.target-signal.entry)/signal.entry)*100;
    const text=[`🚨 فرصة Binance Spot — ${p.symbol.replace("USDT","/USDT")}`,`🧠 ${signal.strategy}${signal.score?` | Score ${signal.score}/100`:""}`,`💰 رصيدك الحر: ${fmt(freeUSDT)} USDT`,`💵 المبلغ المقترح: ${fmt(recommended)} USDT`,`📉 Risk to SL: ${riskPct.toFixed(2)}%`,`📈 Potential to TP: ${profitPct.toFixed(2)}%`,`💲 Entry: ${fmt(signal.entry)}`,`🛑 SL: ${fmt(signal.stop)}`,`🎯 TP: ${fmt(signal.target)}`,`⏱️ صالحة 10 دقائق`,"",canBuy?"⚡ الرصيد متاح — لو موافقة ادخلي من BUY.":"⛔ الحجم الآمن أقل من الحد الأدنى للصفقة.","مفيش شراء تلقائي."].join("\n");
    const rows=[];if(canBuy)rows.push([{text:`✅ BUY ${fmt(recommended)} USDT`,callback_data:`BUY:${id}`}]);rows.push([{text:"📈 افتح Binance Spot",url}]);
    await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text,disable_web_page_preview:true,reply_markup:{inline_keyboard:rows}});await putState(env,promptKey,{sentAt:Date.now(),freeUSDT,recommended,canBuy},SIGNAL_TTL_SEC);
  }
}

async function handleTelegramWebhook(request,env){
  const update=await request.json().catch(()=>null),q=update?.callback_query;if(!q)return new Response("ok");
  const chatId=String(q.message?.chat?.id||"");if(chatId!==String(env.TELEGRAM_CHAT_ID||"")){await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"غير مسموح",show_alert:true});return new Response("ok");}
  const data=String(q.data||"");if(!data.startsWith("BUY:"))return new Response("ok");
  const id=data.slice(4),signal=await getState(env,`live-signal:${id}`);if(!signal||Date.now()>Number(signal.expiresAt||0)){await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"الفرصة انتهت",show_alert:true});return new Response("ok");}
  if(await getState(env,`live-used:${id}`)){await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"تم تنفيذ الزر قبل كده",show_alert:true});return new Response("ok");}
  const latestBalance=await refreshBalance(env),free=Number(latestBalance?.usdt?.free||0),recommended=dynamicQuote(free,Number(signal.entry),Number(signal.stop));
  if(recommended<MIN_ORDER_USDT){await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"الرصيد أو الحجم الآمن مش كفاية",show_alert:true});return new Response("ok");}
  await putState(env,`live-used:${id}`,{startedAt:Date.now()},24*3600);await tg(env,"answerCallbackQuery",{callback_query_id:q.id,text:"جاري تنفيذ BUY على Binance Spot…"});
  try{const r=await executeDirect(env,signal),protection=r.ocoPlaced?`✅ OCO: TP ${fmt(r.tp)} | SL ${fmt(r.stop)}`:`⚠️ الشراء تم لكن OCO فشل: ${r.ocoError||"unknown"}`;await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:[`✅ BUY تم — ${signal.symbol.replace("USDT","/USDT")}`,`💵 المصروف: ${fmt(r.quoteUSDT)} USDT`,`📦 الكمية: ${fmt(r.executedQty)}`,`💲 المتوسط: ${fmt(r.avg)}`,protection].join("\n")});await refreshBalance(env);return new Response("ok");}
  catch(e){await putState(env,`live-used:${id}`,{failedAt:Date.now(),error:String(e?.message||e)},300);await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:`❌ BUY فشل — ${signal.symbol}\n${String(e?.message||e).slice(0,350)}`});return new Response("ok");}
}

export default {
  async fetch(request,env,ctx){
    const url=new URL(request.url);
    if(url.pathname==="/telegram-webhook"&&request.method==="POST") return handleTelegramWebhook(request,env);
    if(url.pathname==="/buy-gateway-status"){const balance=await getState(env,"binance:balance:last");return Response.json({ok:true,mode:"USER_CONFIRMATION_ONLY",autoBuy:false,sizing:"RISK_BASED_DYNAMIC",maxRiskUSDT:MAX_RISK_USDT,maxBalanceFraction:MAX_BALANCE_FRACTION,telegramConfigured:Boolean(env.TELEGRAM_BOT_TOKEN&&env.TELEGRAM_CHAT_ID),balanceBackend:"CLOUDFLARE_DIRECT_BINANCE",executionBackend:"CLOUDFLARE_DIRECT_BINANCE",balanceMonitor:"EVERY_5_MINUTES",freeUSDT:balance?.usdt?.free??null});}
    if(url.pathname==="/balance-refresh"){const balance=await refreshBalance(env);const error=balance?null:await getState(env,"binance:balance:error");return Response.json({ok:Boolean(balance),balance,error,autoBuy:false});}
    return baseWorker.fetch(request,env,ctx);
  },
  async scheduled(event,env,ctx){ctx.waitUntil((async()=>{if(baseWorker.scheduled)await baseWorker.scheduled(event,env,ctx);const bucket=Math.floor(Date.now()/(BALANCE_CHECK_SEC*1000)),key=`balance-check:${bucket}`;if(!await getState(env,key)){await putState(env,key,{at:Date.now()},BALANCE_CHECK_SEC*2);await refreshBalance(env);}await sendPromptForActive(env);})());}
};
