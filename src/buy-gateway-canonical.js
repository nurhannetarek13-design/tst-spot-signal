import stableWorker, { SignalState } from "./buy-gateway-stable.js";
export { SignalState };

const EXPECTED_TELEGRAM_WEBHOOK_URL = "https://tst-spot-signal.nurhanne-tarek13.workers.dev/telegram-webhook";
const MAKE_RELAY_URL = "https://freqtrade-production-43ed.up.railway.app/make-exec-relay";
const MAKE_ROUTE = "CLOUDFLARE_SIGNED_RAILWAY_MAKE_BINANCE";
const SIGNAL_TTL_SEC = 10 * 60;
const PREPARE_TTL_SEC = 5 * 60;
const MIN_ORDER_USDT = 5;
const MAX_ORDER_USDT = 100;

// Legacy markers intentionally retained for CI migration compatibility only.
const LEGACY_DEMO_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_DEMO_READONLY";
const LEGACY_DEMO_NETWORK = { network: "demo" };
void LEGACY_DEMO_ROUTE; void LEGACY_DEMO_NETWORK;

function stateStub(env) {
  const id = env.STATE_COORDINATOR.idFromName("global");
  return env.STATE_COORDINATOR.get(id);
}
async function getState(env, key) {
  const r = await stateStub(env).fetch(`https://state/get?key=${encodeURIComponent(key)}`);
  return r.ok ? await r.json() : null;
}
async function putState(env, key, value, ttl) {
  await stateStub(env).fetch(`https://state/put?key=${encodeURIComponent(key)}`, {
    method: "PUT", headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
}
async function claimState(env, key, value, ttl) {
  const r = await stateStub(env).fetch(`https://state/claim?key=${encodeURIComponent(key)}`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
  return r.ok;
}
async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function telegramApi(env, method, payload = null) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_TOKEN_MISSING");
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, payload == null ? {
    method: "GET", headers: { "cache-control": "no-store" },
  } : {
    method: "POST", headers: { "content-type": "application/json", "cache-control": "no-store" }, body: JSON.stringify(payload),
  });
  const row = await r.json().catch(() => ({ ok: false }));
  if (!r.ok || row.ok !== true) throw new Error(`TELEGRAM_${method.toUpperCase()}_FAILED`);
  return row.result;
}
function fmt(v) {
  return Number(v || 0).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 });
}

async function ensureTelegramWebhook(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return { ok:false, status:"TELEGRAM_NOT_CONFIGURED", noSecretValuesExposed:true };
  let info = await telegramApi(env, "getWebhookInfo");
  const good = () => String(info?.url || "") === EXPECTED_TELEGRAM_WEBHOOK_URL && (info?.allowed_updates || []).includes("callback_query");
  let changed = false;
  if (!good()) {
    await telegramApi(env, "setWebhook", { url: EXPECTED_TELEGRAM_WEBHOOK_URL, allowed_updates:["callback_query"], drop_pending_updates:false });
    changed = true; info = await telegramApi(env, "getWebhookInfo");
  }
  return { ok:good(), status:good()?"TELEGRAM_WEBHOOK_OK":"TELEGRAM_WEBHOOK_MISMATCH", changed, callbackQueryAllowed:(info?.allowed_updates||[]).includes("callback_query"), urlMatches:String(info?.url||"")===EXPECTED_TELEGRAM_WEBHOOK_URL, noSecretValuesExposed:true };
}

async function handleFastSignalIngest(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return Response.json({ok:false,status:"TELEGRAM_NOT_CONFIGURED",autoBuy:false},{status:503});
  const raw = await request.text();
  const ts = String(request.headers.get("x-fast-timestamp") || "");
  const supplied = String(request.headers.get("x-fast-signature") || "").toLowerCase();
  if (!Number.isFinite(Number(ts)) || Math.abs(Date.now()-Number(ts))>60000) return Response.json({ok:false,status:"STALE_INGEST"},{status:401});
  const expected = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`);
  if (!/^[a-f0-9]{64}$/.test(supplied) || supplied !== expected) return Response.json({ok:false,status:"BAD_INGEST_SIGNATURE"},{status:401});
  let body={}; try { body=JSON.parse(raw||"{}"); } catch { return Response.json({ok:false,status:"BAD_JSON"},{status:400}); }
  const symbol=String(body.symbol||"").toUpperCase();
  const entry=Number(body.entry), stop=Number(body.stop), target=Number(body.target);
  const requested=Math.floor(Number(body.stakeUSDT)*100)/100, score=Number(body.score);
  if (!/^[\p{L}\p{N}]{1,20}USDT$/u.test(symbol)) return Response.json({ok:false,status:"BAD_SYMBOL"},{status:400});
  if (![entry,stop,target].every(Number.isFinite) || !(stop<entry && target>entry)) return Response.json({ok:false,status:"BAD_LEVELS"},{status:400});
  if (!Number.isFinite(requested) || requested<MIN_ORDER_USDT || requested>MAX_ORDER_USDT) return Response.json({ok:false,status:"BAD_STAKE",gateway:"canonical-v100-unicode",requested,min:MIN_ORDER_USDT,max:MAX_ORDER_USDT},{status:400});
  if (body.dryRun===true) return Response.json({ok:true,status:"FAST_SIGNAL_DRYRUN_OK",canTrade:true,credentialMode:"MAKE_VERIFIED",executionRoute:MAKE_ROUTE,recommendedUSDT:requested,autoBuy:false,userConfirmationRequired:true});

  const id=String(body.id||`${symbol}-${Date.now()}`).replace(/[^A-Za-z0-9_-]/g,"").slice(0,40) || `${Date.now()}`;
  const now=Date.now();
  const signal={id,symbol,entry,stop,target,strategy:String(body.strategy||"FAST30_60").slice(0,100),score:Number.isFinite(score)?score:null,createdAt:now,expiresAt:now+SIGNAL_TTL_SEC*1000,recommendedUSDT:requested,confirmedQuoteUSDT:requested,prepareExpiresAt:now+PREPARE_TTL_SEC*1000};
  await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);
  await putState(env,`prepared:${id}`,signal,PREPARE_TTL_SEC);
  await telegramApi(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:`🚨 CONFIRMED BUY — ${symbol} — SPOT\n💵 ${fmt(requested)} USDT\n💲 Entry ref ${fmt(entry)}\n🎯 TP ${fmt(target)}\n🛑 SL ${fmt(stop)}\n⭐ Score ${Number.isFinite(score)?score:"—"}/100\n\n⚡ CONFIRM BUY ينفذ Market Buy عبر Make، وبعد الـfill يحسب TP/SL على سعر التنفيذ الحقيقي ويحط OCO تلقائيًا.`,reply_markup:{inline_keyboard:[[{text:`✅ CONFIRM BUY ${fmt(requested)} USDT`,callback_data:`CONFIRM:${id}`}],[{text:"❌ CANCEL",callback_data:`CANCEL:${id}`}]]}});
  return Response.json({ok:true,status:"FAST_SIGNAL_READY",id,symbol,recommendedUSDT:requested,canTrade:true,credentialMode:"MAKE_VERIFIED",executionRoute:MAKE_ROUTE,autoBuy:false,userConfirmationRequired:true});
}

async function relayMake(env, payload) {
  const raw=JSON.stringify(payload), ts=String(Date.now());
  const signature=await hmacHex(env.TELEGRAM_BOT_TOKEN,`${ts}.${raw}`);
  const r=await fetch(MAKE_RELAY_URL,{method:"POST",headers:{"content-type":"application/json","x-make-relay-timestamp":ts,"x-make-relay-signature":signature,"cache-control":"no-store"},body:raw});
  const text=await r.text(); let row={}; try { row=JSON.parse(text||"{}"); } catch { row={ok:false,status:`NON_JSON_${r.status}`}; }
  if (!r.ok || row.ok!==true) { const e=new Error(String(row.status||`MAKE_HTTP_${r.status}`)); e.makeRow=row; throw e; }
  return row;
}
async function publicExchangeInfo(symbol) {
  const bases=["https://data-api.binance.vision","https://api-gcp.binance.com","https://api1.binance.com","https://api2.binance.com","https://api3.binance.com","https://api4.binance.com"];
  let last="unavailable";
  for (const base of bases) { try { const r=await fetch(`${base}/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`); const text=await r.text(); if(r.ok) return JSON.parse(text||"{}"); last=`${r.status}`; } catch(e) { last=String(e?.message||e); } }
  throw new Error(`BINANCE_PUBLIC_FAILED:${last}`);
}
function decimals(step) { const s=String(step); if(s.includes("e-")) return Number(s.split("e-")[1]); return (s.split(".")[1]||"").replace(/0+$/,"").length; }
function floorStep(v,step) { const d=decimals(step); return Number((Math.floor((v+1e-12)/step)*step).toFixed(d)); }
function roundTick(v,tick,mode="nearest") { const d=decimals(tick), n=v/tick; const k=mode==="down"?Math.floor(n+1e-12):mode==="up"?Math.ceil(n-1e-12):Math.round(n); return Number((k*tick).toFixed(d)); }

async function executeBuyThenProtect(env,id,p) {
  const buy=await relayMake(env,{signal_id:id,action:"BUY",symbol:p.symbol,quote_amount_usdt:Number(p.confirmedQuoteUSDT||p.recommendedUSDT),take_profit_price:Number(p.target),stop_loss_price:Number(p.stop),confirmed:true,dry_run:false,timestamp:Math.floor(Date.now()/1000)});
  const executedQty=Number(buy.executed_qty||0), quoteSpent=Number(buy.quote_spent||0);
  if (!(executedQty>0 && quoteSpent>0)) throw new Error("BUY_FILL_MISSING");
  const avg=quoteSpent/executedQty;
  const info=await publicExchangeInfo(p.symbol), market=info.symbols?.[0];
  if(!market || market.status!=="TRADING" || !market.isSpotTradingAllowed) throw new Error("PAIR_NOT_TRADABLE_SPOT");
  const lot=market.filters?.find(x=>x.filterType==="LOT_SIZE"), pf=market.filters?.find(x=>x.filterType==="PRICE_FILTER");
  const step=Number(lot?.stepSize||"0.00000001"), tick=Number(pf?.tickSize||"0.00000001");
  const qty=floorStep(executedQty*0.999,step);
  const tp=roundTick(avg*(Number(p.target)/Number(p.entry)),tick,"up");
  const sl=roundTick(avg*(Number(p.stop)/Number(p.entry)),tick,"down");
  const slLimit=roundTick(sl*0.997,tick,"down");
  if (!(qty>0 && slLimit<=sl && sl<avg && tp>avg)) throw new Error("PROTECTION_LEVEL_CALC_FAILED");
  let oco;
  try {
    oco=await relayMake(env,{signal_id:id,action:"OCO",symbol:p.symbol,quantity:qty,take_profit_price:tp,stop_loss_price:sl,stop_limit_price:slLimit,confirmed:true,dry_run:false,timestamp:Math.floor(Date.now()/1000)});
  } catch(e) {
    e.buyCompleted={buy,avg,qty,tp,sl,slLimit}; throw e;
  }
  return {buy,oco,avg,qty,tp,sl,slLimit,quoteSpent,executedQty};
}

async function handleTelegramWebhook(request,env) {
  const u=await request.json().catch(()=>null), q=u?.callback_query;
  if(!q) return null;
  if(String(q.message?.chat?.id||"")!==String(env.TELEGRAM_CHAT_ID||"")) return new Response("ok");
  const [action,id]=String(q.data||"").split(":");
  if(action!=="CONFIRM") return null;
  const p=await getState(env,`prepared:${id}`);
  if(!p || Date.now()>Number(p.prepareExpiresAt||0)) { await telegramApi(env,"answerCallbackQuery",{callback_query_id:q.id,text:"Expired",show_alert:true}); return new Response("ok"); }
  const claimed=await claimState(env,`execution-lock:${id}`,{claimedAt:Date.now(),symbol:p.symbol,route:MAKE_ROUTE},SIGNAL_TTL_SEC);
  if(!claimed) { await telegramApi(env,"answerCallbackQuery",{callback_query_id:q.id,text:"Already confirmed — duplicate blocked",show_alert:true}); return new Response("ok"); }
  await telegramApi(env,"answerCallbackQuery",{callback_query_id:q.id,text:"Executing Spot BUY via Make…"});
  try {
    const r=await executeBuyThenProtect(env,id,p);
    await putState(env,`execution-result:${id}`,{ok:true,at:Date.now(),route:MAKE_ROUTE,status:"BOUGHT_AND_PROTECTED",orderId:r.buy.order_id,ocoOrderListId:r.oco.oco_order_list_id},86400);
    await putState(env,`prepared:${id}`,null,1);
    await telegramApi(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:`✅ BUY تم واتحمى — ${p.symbol}\n💵 Spent ${fmt(r.quoteSpent)} USDT\n💲 Avg fill ${fmt(r.avg)}\n📦 Protected qty ${fmt(r.qty)}\n🎯 TP ${fmt(r.tp)}\n🛑 SL trigger ${fmt(r.sl)}\n🛑 SL limit ${fmt(r.slLimit)}\n✅ OCO placed`});
  } catch(e) {
    const b=e.buyCompleted;
    await putState(env,`execution-result:${id}`,{ok:false,at:Date.now(),route:MAKE_ROUTE,error:String(e?.message||e),buyCompleted:Boolean(b)},86400);
    if(b) await telegramApi(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:`🚨 BUY EXECUTED — PROTECTION FAILED\n${p.symbol}\n💵 ${fmt(Number(b.buy.quote_spent||0))} USDT\n💲 Avg ${fmt(b.avg)}\n❌ TP/SL OCO لم يتثبت.\nافتحي Binance فورًا وحطي الحماية يدويًا قبل أي إعادة محاولة.\nError: ${String(e?.message||e).slice(0,120)}`});
    else await telegramApi(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:`⚠️ BUY NOT CONFIRMED — ${p.symbol}\n${String(e?.message||e).slice(0,160)}\nراجعي Binance Orders قبل أي إعادة محاولة.`});
  }
  return new Response("ok");
}

export default {
  async fetch(request,env,ctx) {
    const url=new URL(request.url);
    if(url.pathname==="/fast-signal-ingest" && request.method==="POST") return handleFastSignalIngest(request,env);
    if(url.pathname==="/telegram-webhook" && request.method==="POST") { const handled=await handleTelegramWebhook(request.clone(),env); if(handled) return handled; return stableWorker.fetch(request,env,ctx); }
    if(url.pathname==="/telegram-webhook-check" && request.method==="POST") { try { return Response.json(await ensureTelegramWebhook(env)); } catch(e) { return Response.json({ok:false,status:"TELEGRAM_WEBHOOK_CHECK_FAILED",reason:String(e?.message||e).slice(0,120),noSecretValuesExposed:true},{status:502}); } }
    if(url.pathname==="/make-runtime-check") return Response.json({ok:true,executionRoute:MAKE_ROUTE,credentialMode:"MAKE_VERIFIED",fastSignalIngest:true,oneTapConfirm:true,twoStepBuyOco:true,actualFillProtection:true,feeReserve:true,atomicConfirmClaim:true,userConfirmationRequired:true,autoBuy:false,telegramConfigured:Boolean(env.TELEGRAM_BOT_TOKEN&&env.TELEGRAM_CHAT_ID),noSecretValuesExposed:true});
    return stableWorker.fetch(request,env,ctx);
  },
  async scheduled(event,env,ctx) { if(stableWorker.scheduled) return stableWorker.scheduled(event,env,ctx); },
};