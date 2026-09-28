import baseWorker, { SignalState } from "./edge-worker.js";
import { readLivePolicy } from "./live-cutover-policy.js";
import {
  manualBuyAndProtect,
  executionReadOnlyHeartbeat,
  executionProvider,
  executionRoute,
  executorConfigured,
  executionOwner,
  executionDryRun,
} from "./live-execution-router.js";
export { SignalState };

const SIGNAL_TTL_SEC = 10 * 60;
const PREPARE_TTL_SEC = 5 * 60;
const MAX_SIGNAL_AGE_MS = 10 * 60 * 1000;
const MIN_ORDER_USDT = 5;
const MAX_BALANCE_FRACTION = 0.80;
const MAX_RISK_USDT = 0.20;
const EXPECTED_TELEGRAM_WEBHOOK_URL = "https://tst-spot-signal.nurhanne-tarek13.workers.dev/telegram-webhook";

function creds(env) {
  const route = executionRoute(env);
  const configured = executorConfigured(env);
  const relayReady = Boolean(env.TELEGRAM_BOT_TOKEN) && configured;
  return {
    network: relayReady ? "production" : "none",
    credentialMode: relayReady ? "LIVE" : "MISSING",
    route,
    provider: executionProvider(env),
  };
}

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
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
}

async function claimState(env, key, value, ttl) {
  const r = await stateStub(env).fetch(`https://state/claim?key=${encodeURIComponent(key)}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
  return r.ok;
}

async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}



function riskCappedQuote(entry, stop, requested = 5.5) {
  const e = Number(entry), s = Number(stop), req = Number(requested);
  const stopPct = (e - s) / e;
  if (!(e > 0 && s > 0 && stopPct > 0)) throw new Error("INVALID_STOP_DISTANCE");
  const riskSized = MAX_RISK_USDT / stopPct;
  const requestCap = Number.isFinite(req) && req > 0 ? req : 5.5;
  return Math.floor(Math.min(riskSized, requestCap, 5.5) * 100) / 100;
}

async function executionOperational(env, symbol = null) {
  const now = Date.now();
  const [ops, reconciliationState, bridge, ownership, daily, unknownState, unprotectedState] = await Promise.all([
    getState(env, "ops:state"),
    getState(env, "ops:reconciliation:last"),
    getState(env, "bridge:health"),
    getState(env, "bridge:ownership"),
    getState(env, "risk:daily-live"),
    getState(env, "live:unknown-orders"),
    getState(env, "live:unprotected-positions"),
  ]);
  const fresh = (at, maxMs = 20 * 60 * 1000) => Number(at || 0) > 0 && now - Number(at || 0) <= maxMs;
  const ready = executorConfigured(env);
  const provider = executionProvider(env);
  const policy = readLivePolicy(env);
  const unknownCount = Array.isArray(unknownState) ? unknownState.length : Number(unknownState?.count || 0);
  const unprotectedCount = Array.isArray(unprotectedState) ? unprotectedState.length : Number(unprotectedState?.count || 0);
  const realizedLoss = Math.abs(Number(daily?.realizedLossUSDT || 0));

  if (policy.emergencyKillSwitch === true) {
    return {
      ok:false,
      provider,
      state:String(ops?.state || "UNKNOWN"),
      blocker:"EMERGENCY_KILL_SWITCH",
      executorConfigured:ready,
    };
  }
  if (realizedLoss >= Math.abs(Number(policy.dailyLossCapUSDT || 0))) {
    return {
      ok:false,
      provider,
      state:String(ops?.state || "UNKNOWN"),
      blocker:"DAILY_LOSS_CAP_REACHED",
      executorConfigured:ready,
    };
  }
  if (unknownCount > 0) {
    return { ok:false, provider, state:String(ops?.state || "UNKNOWN"), blocker:"UNKNOWN_ORDERS_PRESENT", executorConfigured:ready };
  }
  if (unprotectedCount > 0) {
    return { ok:false, provider, state:String(ops?.state || "UNKNOWN"), blocker:"UNPROTECTED_POSITION_PRESENT", executorConfigured:ready };
  }

  if (provider === "SUPABASE_V2") {
    const [heartbeat, liveReconciliation] = await Promise.all([
      executionReadOnlyHeartbeat(env),
      executionReadOnlyReconcile(env),
    ]);
    const transportOk = heartbeat?.transportOk === true
      && heartbeat?.body?.canTrade === true
      && heartbeat?.body?.financialAction === false;
    const reconciliationOk = liveReconciliation?.ok === true
      && liveReconciliation?.noUnknownOrders === true
      && liveReconciliation?.noUnprotectedPositions === true;
    const openPositions = Number(liveReconciliation?.protectedOrderLists || 0);
    const sameSymbolOpen = symbol
      ? (liveReconciliation?.botOpenSymbols || []).includes(String(symbol).toUpperCase())
      : false;
    const capacityOk = openPositions < Number(policy.maxOpenPositions || 1);
    const stateFresh = reconciliationState?.ok === true && fresh(reconciliationState?.at);
    const ok = ready
      && ops?.state === "HEALTHY"
      && stateFresh
      && transportOk
      && reconciliationOk
      && capacityOk
      && !sameSymbolOpen;
    return {
      ok,
      provider,
      state:String(ops?.state || "UNKNOWN"),
      reconciliationOk:stateFresh && reconciliationOk,
      bridgeOk:transportOk,
      ownershipOk:transportOk,
      executorConfigured:ready,
      openPositions,
      maxOpenPositions:Number(policy.maxOpenPositions || 1),
      sameSymbolOpen,
      dailyLossUSDT:realizedLoss,
      dailyLossCapUSDT:Number(policy.dailyLossCapUSDT || 0),
      heartbeat,
      liveReconciliation,
      blocker:!ready ? "EXECUTOR_NOT_READY"
        : ops?.state !== "HEALTHY" ? "OPS_NOT_HEALTHY"
        : !stateFresh ? "STALE_RECONCILIATION"
        : !transportOk ? "BINANCE_READONLY_UNHEALTHY"
        : !reconciliationOk ? "LIVE_RECONCILIATION_BLOCKED"
        : !capacityOk ? "MAX_OPEN_POSITIONS"
        : sameSymbolOpen ? "DUPLICATE_SYMBOL_POSITION"
        : null,
    };
  }

  return {
    ok:false,
    provider,
    state:String(ops?.state || "UNKNOWN"),
    blocker:"LEGACY_FINANCIAL_ROUTE_DISABLED",
    reconciliationOk:false,
    bridgeOk:bridge?.ok === true && fresh(bridge?.at),
    ownershipOk:ownership?.owner === executionOwner(env) && ownership?.exclusive === true && fresh(ownership?.at),
    executorConfigured:ready,
  };
}

async function prepareManualE2EPrompt(env) {
  const armed = String(env.E2E_ARMED || "").toLowerCase() === "true";
  const liveEnabled = String(env.LIVE_EXECUTION_ENABLED || "").toLowerCase() === "true";
  const autonomous = String(env.AUTONOMOUS_ENABLED || "").toLowerCase() === "true";
  if (!armed || !liveEnabled || autonomous) return { ok:false, status:"E2E_PROMPT_NOT_ARMED" };
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return { ok:false, status:"TELEGRAM_NOT_CONFIGURED" };

  const completed = await getState(env, "cutover:e2e:result");
  if (completed?.ok === true && completed?.manual === true) return { ok:true, status:"MANUAL_E2E_ALREADY_COMPLETE" };

  const recent = await getState(env, "cutover:e2e:prompt");
  if (recent?.sentAt && Date.now() - Number(recent.sentAt) < SIGNAL_TTL_SEC * 1000) {
    return { ok:true, status:"MANUAL_E2E_PROMPT_ALREADY_SENT", id:recent.id };
  }

  const health = await executionOperational(env);
  if (!health.ok) return { ok:false, status:"EXECUTION_OPERATIONAL_GATE_FAILED", health };

  const claimed = await claimState(env, "cutover:e2e:prompt-lock", { at:Date.now() }, 60);
  if (!claimed) return { ok:true, status:"MANUAL_E2E_PROMPT_LOCKED" };

  const symbol = "SOLUSDT";
  const book = await publicBinance(`/api/v3/ticker/bookTicker?symbol=${encodeURIComponent(symbol)}`);
  const ask = Number(book.askPrice || 0), bid = Number(book.bidPrice || 0);
  if (!(ask > 0 && bid > 0 && ask >= bid)) return { ok:false, status:"E2E_BOOK_UNAVAILABLE" };
  const spread = (ask - bid) / ((ask + bid) / 2);
  if (spread > 0.0012) return { ok:false, status:"E2E_SPREAD_TOO_WIDE" };

  const entry = ask;
  const stop = roundTo(entry * 0.992, 0.01);
  const target = roundTo(entry * 1.01, 0.01);
  const rec = riskCappedQuote(entry, stop, 5.5);
  if (rec < MIN_ORDER_USDT) return { ok:false, status:"E2E_SIZE_BELOW_MIN" };

  const now = Date.now();
  const id = `CUTOVERE2E-${now}`;
  const signal = {
    id,
    symbol,
    entry,
    stop,
    target,
    strategy:"CUTOVER_MANUAL_E2E",
    score:100,
    createdAt:now,
    expiresAt:now + SIGNAL_TTL_SEC * 1000,
    recommendedUSDT:rec,
    confirmedQuoteUSDT:rec,
    prepareExpiresAt:now + PREPARE_TTL_SEC * 1000,
  };
  await putState(env, `live-signal:${id}`, signal, SIGNAL_TTL_SEC);
  await putState(env, `prepared:${id}`, signal, PREPARE_TTL_SEC);
  await tg(env, "sendMessage", {
    chat_id:String(env.TELEGRAM_CHAT_ID),
    text:`🧪 MANUAL LIVE E2E — ${symbol}\n💵 ${fmt(rec)} USDT\n💲 Entry ref ${fmt(entry)}\n🎯 TP ${fmt(target)}\n🛑 SL ${fmt(stop)}\n\n⚠️ الاختبار لن ينفذ أي شراء إلا بعد ضغطك CONFIRM BUY.`,
    reply_markup:{inline_keyboard:[[{text:`✅ CONFIRM E2E BUY ${fmt(rec)} USDT`,callback_data:`CONFIRM:${id}`}],[{text:"❌ CANCEL",callback_data:`CANCEL:${id}`}]]},
  });
  await putState(env, "cutover:e2e:prompt", {
    id,
    symbol,
    sentAt:now,
    quoteUSDT:rec,
    automaticExecution:false,
    executionRoute:executionProvider(env),
  }, SIGNAL_TTL_SEC);
  return { ok:true, status:"MANUAL_E2E_PROMPT_SENT", id, symbol, quoteUSDT:rec };
}


async function handleFastSignalIngest(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    return Response.json({ ok:false, status:"TELEGRAM_NOT_CONFIGURED", autoBuy:false }, {status:503});
  }
  const raw = await request.text();
  const ts = String(request.headers.get("x-fast-timestamp") || "");
  const supplied = String(request.headers.get("x-fast-signature") || "").toLowerCase();
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) {
    return Response.json({ok:false,status:"STALE_INGEST"},{status:401});
  }
  const expected = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`);
  if (!/^[a-f0-9]{64}$/.test(supplied) || supplied !== expected) {
    return Response.json({ok:false,status:"BAD_INGEST_SIGNATURE"},{status:401});
  }
  let body={};
  try { body=JSON.parse(raw || "{}"); } catch { return Response.json({ok:false,status:"BAD_JSON"},{status:400}); }
  const symbol=String(body.symbol||"").toUpperCase();
  const entry=Number(body.entry), stop=Number(body.stop), target=Number(body.target), requested=Number(body.stakeUSDT);
  const score=Number(body.score);
  if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) return Response.json({ok:false,status:"BAD_SYMBOL"},{status:400});
  if (![entry,stop,target].every(Number.isFinite) || !(stop < entry && target > entry)) return Response.json({ok:false,status:"BAD_LEVELS"},{status:400});
  if (!Number.isFinite(requested) || requested < MIN_ORDER_USDT || requested > 10) return Response.json({ok:false,status:"BAD_STAKE"},{status:400});

  const rec=riskCappedQuote(entry,stop,requested);
  if (rec < MIN_ORDER_USDT) return Response.json({ok:false,status:"SIZE_TOO_SMALL",autoBuy:false},{status:409});

  if (body.dryRun === true) {
    if (executionProvider(env) !== "SUPABASE_V2") {
      return Response.json({ok:false,status:"DRYRUN_REQUIRES_SUPABASE_V2",financialAction:false},{status:409});
    }
    const rawId=String(body.id||`DRY-${symbol}-${Math.trunc(entry*1e8)}-${Math.trunc(stop*1e8)}`);
    const id=rawId.replace(/[^A-Za-z0-9_-]/g,"").slice(0,40) || compactId({symbol,entry,createdAt:Date.now()});
    const report=await executionDryRun(env,{
      signal_id:id,
      symbol,
      entry_price:entry,
      quote_amount_usdt:rec,
      take_profit_price:target,
      stop_loss_price:stop,
    });
    return Response.json({
      ...report,
      source:"FAST_SIGNAL_INGEST",
      productionRoute:true,
      userConfirmationRequired:false,
      autoBuy:false,
    },{status:report?.ok?200:503,headers:{"cache-control":"no-store"}});
  }

  const c = creds(env);
  if (c.credentialMode !== "LIVE") {
    return Response.json({ ok:false, status:"LIVE_CREDENTIALS_REQUIRED", credentialMode:c.credentialMode, autoBuy:false }, {status:503});
  }
  const health = await executionOperational(env);
  if (!health.ok) {
    return Response.json({ok:false,status:"EXECUTION_OPERATIONAL_GATE_FAILED",health,autoBuy:false},{status:503});
  }
  const rawId=String(body.id||`${symbol}-${Date.now()}`);
  const id=rawId.replace(/[^A-Za-z0-9_-]/g,"").slice(0,40) || compactId({symbol,entry,createdAt:Date.now()});
  const now=Date.now();
  const signal={id,symbol,entry,stop,target,strategy:String(body.strategy||"FAST30_60").slice(0,100),score:Number.isFinite(score)?score:null,createdAt:now,expiresAt:now+SIGNAL_TTL_SEC*1000,recommendedUSDT:rec,confirmedQuoteUSDT:rec,prepareExpiresAt:now+PREPARE_TTL_SEC*1000};
  await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);
  await putState(env,`prepared:${id}`,signal,PREPARE_TTL_SEC);
  await tg(env,"sendMessage",{
    chat_id:String(env.TELEGRAM_CHAT_ID),
    text:`🚨 CONFIRMED BUY — ${symbol} — SPOT\n💵 ${fmt(rec)} USDT\n💲 Entry ref ${fmt(entry)}\n🎯 TP ${fmt(target)}\n🛑 SL ${fmt(stop)}\n⭐ Score ${Number.isFinite(score)?score:"—"}/100\n\n⚡ ضغطة CONFIRM BUY تنفذ Market Buy حقيقي ثم تحط TP/SL تلقائيًا.`,
    reply_markup:{inline_keyboard:[[{text:`✅ CONFIRM BUY ${fmt(rec)} USDT`,callback_data:`CONFIRM:${id}`}],[{text:"❌ CANCEL",callback_data:`CANCEL:${id}`}]]}
  });
  await putState(env,`buy-prompt:${id}`,{sentAt:now,source:"FAST_INGEST"},SIGNAL_TTL_SEC);
  return Response.json({ok:true,status:"FAST_SIGNAL_READY",id,symbol,recommendedUSDT:rec,canTrade:true,credentialMode:"LIVE",autoBuy:false,userConfirmationRequired:true});
}

async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const row = await r.json().catch(() => ({ ok: false }));
  if (!r.ok || row.ok === false) throw new Error(`Telegram ${method} failed`);
  return row;
}

function fmt(v) {
  return Number(v || 0).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 });
}

function compactId(p) {
  const raw = `${p.symbol}:${p.openedAt || p.createdAt || Date.now()}:${p.entry}`;
  let h = 2166136261;
  for (let i = 0; i < raw.length; i++) {
    h ^= raw.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(36);
}

function dynamicQuote(freeUSDT, entry, stop, requested = null) {
  const stopPct = (entry - stop) / entry;
  if (!(stopPct > 0)) throw new Error("INVALID_STOP_DISTANCE");
  const riskSized = MAX_RISK_USDT / stopPct;
  const balanceCap = freeUSDT * MAX_BALANCE_FRACTION;
  let size = Math.min(riskSized, balanceCap, 10);
  const req = Number(requested);
  if (Number.isFinite(req) && req > 0) size = Math.min(size, req);
  return Math.floor(size * 100) / 100;
}

function decimals(step) {
  const s = String(step);
  if (s.includes("e-")) return Number(s.split("e-")[1]);
  return (s.split(".")[1] || "").replace(/0+$/, "").length;
}

function floorTo(value, step) {
  if (!step || step <= 0) return value;
  return Number((Math.floor((value + 1e-12) / step) * step).toFixed(decimals(step)));
}

function roundTo(value, tick) {
  if (!tick || tick <= 0) return value;
  return Number((Math.round(value / tick) * tick).toFixed(decimals(tick)));
}

async function publicBinance(path) {
  const bases = [
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
  ];
  let last = "unavailable";
  for (const base of bases) {
    try {
      const r = await fetch(base + path);
      const text = await r.text();
      if (r.ok) return JSON.parse(text || "{}");
      last = `${r.status} ${text.slice(0, 300)}`;
    } catch (e) {
      last = String(e?.message || e);
    }
  }
  throw new Error(`BINANCE_PUBLIC_FAILED: ${last}`);
}

async function executionPriceGate(symbol, referenceEntry, referenceStop, referenceTarget) {
  const book = await publicBinance(`/api/v3/ticker/bookTicker?symbol=${encodeURIComponent(symbol)}`);
  const ask=Number(book.askPrice||0), bid=Number(book.bidPrice||0);
  const ref=Number(referenceEntry||0), stop=Number(referenceStop||0), target=Number(referenceTarget||0);
  if(!(ask>0 && bid>0 && ask>=bid && ref>0 && stop>0 && target>ref)) throw new Error("EXECUTION_BOOK_INVALID");
  const mid=(ask+bid)/2;
  const spread=(ask-bid)/mid;
  const deviation=ask/ref-1;
  if(spread>0.0012) throw new Error(`EXECUTION_SPREAD_TOO_WIDE:${(spread*100).toFixed(3)}%`);
  if(ask>=target) throw new Error("SETUP_ALREADY_AT_OR_ABOVE_TARGET");
  if(ask<=stop) throw new Error("SETUP_ALREADY_AT_OR_BELOW_STOP");
  if(deviation>0.0035) throw new Error(`STALE_PRICE_CHASE:${(deviation*100).toFixed(3)}%`);
  if(deviation<-0.0060) throw new Error(`SETUP_DETERIORATED:${(deviation*100).toFixed(3)}%`);
  return {ask,bid,spreadPct:spread*100,deviationPct:deviation*100};
}

async function signedBinance() {
  throw new Error("LEGACY_VERCEL_SIGNER_REVOKED");
}

function safeRelayDiagnostic(errorText) {
  const s=String(errorText||"");
  if (s.includes("-2015")) return "BINANCE_CREDENTIAL_OR_IP_REJECTED";
  if (s.includes("-1022") && s.includes("signer=RSA_SHA256")) return "BINANCE_SIGNATURE_REJECTED_RSA";
  if (s.includes("-1022") && s.includes("signer=ED25519")) return "BINANCE_SIGNATURE_REJECTED_ED25519";
  if (s.includes("-1022") && s.includes("signer=HMAC_SHA256")) return "BINANCE_SIGNATURE_REJECTED_HMAC";
  if (s.includes("-1022")) return "BINANCE_SIGNATURE_REJECTED";
  if (s.includes("-1021")) return "BINANCE_CLOCK_REJECTED";
  if (s.includes("BAD_SIGNED_REQUEST")) return "RELAY_SIGNED_REQUEST_REJECTED";
  if (s.includes("BAD_RELAY_SIGNATURE")) return "RELAY_HMAC_REJECTED";
  if (s.includes("VERCEL_BINANCE_CREDENTIALS_MISSING")) return "VERCEL_BINANCE_CREDENTIALS_MISSING";
  if (s.includes("PRODUCTION_ONLY")) return "RELAY_NETWORK_REJECTED";
  if (s.includes("BINANCE_UPSTREAM_REJECTED")) return "BINANCE_UPSTREAM_REJECTED";
  return s ? "ACCOUNT_PREFLIGHT_FAILED" : "NONE";
}

export function evaluateCloudflareAccountSafety(restrictions = {}, accountStatus = {}, apiTradingStatus = {}) {
  const reasons=[];
  const account=String(accountStatus?.data ?? accountStatus?.status ?? "").trim();
  const trading=(apiTradingStatus?.data && typeof apiTradingStatus.data==="object")
    ? apiTradingStatus.data
    : (apiTradingStatus || {});

  if (restrictions.enableReading !== true) reasons.push("READ_PERMISSION_REQUIRED");
  if (restrictions.enableSpotAndMarginTrading !== true) reasons.push("SPOT_TRADING_PERMISSION_REQUIRED");
  if (restrictions.enableWithdrawals === true) reasons.push("WITHDRAWALS_MUST_BE_DISABLED");
  if (restrictions.enableFutures === true) reasons.push("FUTURES_MUST_BE_DISABLED");
  if (restrictions.enableMargin === true) reasons.push("MARGIN_MUST_BE_DISABLED");
  if (restrictions.ipRestrict !== true) reasons.push("IP_RESTRICTION_REQUIRED");
  if (account.toLowerCase() !== "normal") reasons.push("ACCOUNT_STATUS_NOT_NORMAL");
  if (trading?.isLocked === true) reasons.push("API_TRADING_LOCKED");

  return {
    ok:reasons.length===0,
    reasons,
    accountStatus:account || null,
    isLocked:trading?.isLocked === true,
    plannedRecoverTime:Number(trading?.plannedRecoverTime || 0) || 0,
    ipRestrict:restrictions.ipRestrict === true,
    withdrawalsDisabled:restrictions.enableWithdrawals !== true,
    futuresDisabled:restrictions.enableFutures !== true,
    marginDisabled:restrictions.enableMargin !== true,
    spotPermission:restrictions.enableSpotAndMarginTrading === true,
    checkedAt:Date.now(),
  };
}

async function refreshAccountSafety(env, force = false) {
  if (!force) {
    const cached=await getState(env,"binance:account-safety:last");
    if (cached?.checkedAt && Date.now()-Number(cached.checkedAt)<5*60*1000) return cached;
  }
  try {
    const [restrictions, accountStatus, apiTradingStatus]=await Promise.all([
      signedBinance(env,"GET","/sapi/v1/account/apiRestrictions",{}),
      signedBinance(env,"GET","/sapi/v1/account/status",{}),
      signedBinance(env,"GET","/sapi/v1/account/apiTradingStatus",{}),
    ]);
    const result=evaluateCloudflareAccountSafety(restrictions,accountStatus,apiTradingStatus);
    await putState(env,"binance:account-safety:last",result,10*60);
    return result;
  } catch (e) {
    const result={
      ok:false,
      reasons:["ACCOUNT_SAFETY_UNAVAILABLE"],
      diagnostic:safeRelayDiagnostic(String(e?.message||e)),
      checkedAt:Date.now(),
    };
    await putState(env,"binance:account-safety:last",result,2*60);
    return result;
  }
}

async function refreshBalance(env) {
  const c = creds(env);
  try {
    const [account, accountSafety] = await Promise.all([
      signedBinance(env, "GET", "/api/v3/account", {}),
      refreshAccountSafety(env, false),
    ]);
    const balances = (account.balances || [])
      .map((b) => ({ asset: b.asset, free: Number(b.free || 0), locked: Number(b.locked || 0) }))
      .filter((b) => b.free > 0 || b.locked > 0);
    const usdt = balances.find((b) => b.asset === "USDT") || { asset: "USDT", free: 0, locked: 0 };
    const balance = {
      ok: true,
      status: "ACCOUNT_BALANCE_OK",
      canTrade: Boolean(account.canTrade),
      accountSafetyOk: accountSafety?.ok === true,
      accountSafetyReasons: accountSafety?.reasons || ["ACCOUNT_SAFETY_UNAVAILABLE"],
      usdt: { free: usdt.free, locked: usdt.locked, total: usdt.free + usdt.locked },
      nonZeroAssets: balances,
      source: c.route,
      network: c.network,
      credentialMode: c.credentialMode,
      checkedAt: Date.now(),
    };
    await putState(env, "binance:balance:last", balance, 7200);
    await putState(env, "binance:balance:error", null, 60);
    return balance;
  } catch (e) {
    await putState(env, "binance:balance:error", {
      at: Date.now(),
      credentialMode: c.credentialMode,
      route: c.route,
      error: String(e?.message || e),
    }, 1800);
    return null;
  }
}

async function requireFreshExecutionRoute(env) {
  if (executionProvider(env) === "SUPABASE_V2") {
    const heartbeat = await executionReadOnlyHeartbeat(env);
    if (heartbeat?.transportOk !== true) {
      throw new Error("SUPABASE_V2_FRESH_PREFLIGHT_TRANSPORT_FAILED");
    }
    if (heartbeat?.body?.canTrade !== true || heartbeat?.body?.financialAction !== false) {
      throw new Error("SUPABASE_V2_FRESH_PREFLIGHT_NOT_VERIFIED");
    }
    return {
      ok: true,
      provider: "SUPABASE_V2",
      routeVersion: String(heartbeat?.body?.routeVersion || "supabase-v2"),
      checkedAt: Date.now(),
    };
  }

  const startedAt = Date.now();
  const watchdog = await executionReadOnlyHeartbeat(env);
  if (watchdog?.transportOk !== true) {
    throw new Error("MAKE_V2_FRESH_PREFLIGHT_TRANSPORT_FAILED");
  }
  const [buyAudit, ocoAudit] = await Promise.all([
    getState(env, "bridge:route:BUY_V2"),
    getState(env, "bridge:route:OCO_V2"),
  ]);
  const freshAccepted = (row) =>
    row?.lastStatus === "BRIDGE_AUTH_OK"
    && Number(row?.acceptedAt || 0) >= startedAt - 1000
    && Date.now() - Number(row?.acceptedAt || 0) <= 30_000;
  if (!freshAccepted(buyAudit) || !freshAccepted(ocoAudit)) {
    throw new Error("MAKE_V2_FRESH_PREFLIGHT_NOT_EXECUTED");
  }
  return {
    ok:true,
    provider:"MAKE_V2",
    buyAcceptedAt:Number(buyAudit.acceptedAt || 0),
    ocoAcceptedAt:Number(ocoAudit.acceptedAt || 0),
    routeVersion:String(buyAudit.routeVersion || ocoAudit.routeVersion || "v2"),
  };
}

async function executeConfirmedBuy(env, s) {
  const c = creds(env);
  if (c.credentialMode !== "LIVE") throw new Error("LIVE_CREDENTIALS_REQUIRED");
  if (!executorConfigured(env)) {
    throw new Error("EXECUTOR_NOT_READY");
  }
  const operational = await executionOperational(env, String(s.symbol || "").toUpperCase());
  if (!operational.ok) {
    throw new Error("EXECUTION_OPERATIONAL_GATE_FAILED");
  }
  await requireFreshExecutionRoute(env);

  const symbol = String(s.symbol || "").toUpperCase();
  if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) throw new Error("BAD_SYMBOL");
  const createdAt = Number(s.createdAt || 0);
  if (!Number.isFinite(createdAt) || Date.now() - createdAt > MAX_SIGNAL_AGE_MS || createdAt > Date.now() + 30_000) {
    throw new Error("STALE_SIGNAL");
  }
  const entryRef = Number(s.entry), stopRef = Number(s.stop), targetRef = Number(s.target);
  if (![entryRef, stopRef, targetRef].every(Number.isFinite) || !(stopRef < entryRef && targetRef > entryRef)) {
    throw new Error("INVALID_TP_SL_GEOMETRY");
  }

  const info = await publicBinance(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`);
  const market = info.symbols?.[0];
  if (!market || market.status !== "TRADING" || !market.isSpotTradingAllowed || market.quoteAsset !== "USDT") {
    throw new Error("PAIR_NOT_TRADABLE_SPOT");
  }
  await executionPriceGate(symbol, entryRef, stopRef, targetRef);

  const requested = Number(s.confirmedQuoteUSDT || s.recommendedUSDT || 0);
  if (!Number.isFinite(requested) || requested < 5) throw new Error("SIZE_TOO_SMALL");
  const quoteUSDT = Math.floor(Math.min(requested, 5.5) * 100) / 100;

  const result = await manualBuyAndProtect(env, {
    signal_id: String(s.id || ""),
    symbol,
    quote_amount_usdt: quoteUSDT,
    take_profit_price: targetRef,
    stop_loss_price: stopRef,
  });

  if (result?.status === "OCO_PLACED") {
    return {
      ok: true,
      status: "BOUGHT_AND_PROTECTED",
      symbol,
      quoteUSDT: Number(result?.buy?.body?.quote_spent || quoteUSDT),
      recommendedUSDT: quoteUSDT,
      executedQty: Number(result?.executedQty || 0),
      avg: Number(result?.buy?.body?.weighted_price || entryRef),
      tp: targetRef,
      stop: stopRef,
      ocoPlaced: true,
      emergencyClosed: false,
      autoBuy: false,
      userConfirmed: true,
      executionRoute: executionProvider(env),
      buyOrderId: Number(result?.buy?.body?.order_id || 0) || null,
      ocoOrderListId: Number(result?.oco?.body?.oco_order_list_id || 0) || null,
      clientIds: result?.clientIds || null,
    };
  }

  if (result?.status === "PROTECTION_FAILED_EMERGENCY_CLOSED") {
    return {
      ok: true,
      status: "PROTECTION_FAILED_EMERGENCY_CLOSED",
      symbol,
      quoteUSDT: Number(result?.buy?.body?.quote_spent || quoteUSDT),
      recommendedUSDT: quoteUSDT,
      executedQty: Number(result?.executedQty || 0),
      avg: Number(result?.buy?.body?.weighted_price || entryRef),
      tp: targetRef,
      stop: stopRef,
      ocoPlaced: false,
      emergencyClosed: true,
      autoBuy: false,
      userConfirmed: true,
      executionRoute: executionProvider(env),
      buyOrderId: Number(result?.buy?.body?.order_id || 0) || null,
      emergencyOrderId: Number(result?.oco?.body?.emergency_order_id || 0) || null,
      clientIds: result?.clientIds || null,
    };
  }

  const status = String(result?.status || "EXECUTION_FAILED");
  if (result?.reconciliationRequired === true) {
    const currentUnknown = await getState(env, "live:unknown-orders");
    const rows = Array.isArray(currentUnknown) ? currentUnknown : [];
    rows.push({
      signalId: String(s.id || ""),
      symbol,
      status,
      route: executionProvider(env),
      at: Date.now(),
    });
    await putState(env, "live:unknown-orders", rows.slice(-20), 30 * 24 * 60 * 60);
    throw new Error(`${status}:RECONCILIATION_REQUIRED:NO_RESEND`);
  }
  throw new Error(status);
}

async function sendPromptForActive(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  const c = creds(env);
  if (c.credentialMode !== "LIVE") return;

  const active = (await getState(env, "paper:active")) || [];
  const health = await executionOperational(env);
  if (!health.ok) return;
  for (const p of active) {
    const id = compactId(p);
    if (await getState(env, `buy-prompt:${id}`)) continue;
    const s = {
      id,
      symbol: p.symbol,
      entry: Number(p.entry),
      stop: Number(p.stop),
      target: Number(p.target),
      strategy: p.strategy || "Scanner",
      score: p.score || null,
      createdAt: Date.now(),
      expiresAt: Date.now() + SIGNAL_TTL_SEC * 1000,
    };
    if (!(s.stop < s.entry && s.target > s.entry)) continue;
    const rec = riskCappedQuote(s.entry, s.stop, 5.5);
    s.recommendedUSDT = rec;
    await putState(env, `live-signal:${id}`, s, SIGNAL_TTL_SEC);
    if (rec >= MIN_ORDER_USDT) {
      await tg(env, "sendMessage", {
        chat_id: String(env.TELEGRAM_CHAT_ID),
        text: `🚨 ${p.symbol}\n💵 ${fmt(rec)} USDT\n💲 Entry ${fmt(s.entry)}\n🎯 TP ${fmt(s.target)}\n🛑 SL ${fmt(s.stop)}\n⭐ Score ${s.score ?? "—"}/100`,
        reply_markup: { inline_keyboard: [[{ text: `🧾 PREPARE ${fmt(rec)} USDT`, callback_data: `PREP:${id}` }]] },
      });
    }
    await putState(env, `buy-prompt:${id}`, { sentAt: Date.now() }, SIGNAL_TTL_SEC);
  }
}

async function ensureTelegramWebhook(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    return { ok:false, status:"TELEGRAM_NOT_CONFIGURED", noSecretValuesExposed:true };
  }
  const info = await tg(env, "getWebhookInfo", {});
  const current = info?.result || info || {};
  const allowed = Array.isArray(current.allowed_updates) ? current.allowed_updates : [];
  const matches = String(current.url || "") === EXPECTED_TELEGRAM_WEBHOOK_URL;
  const callbackAllowed = allowed.includes("callback_query");

  if (!matches || !callbackAllowed) {
    await tg(env, "setWebhook", {
      url: EXPECTED_TELEGRAM_WEBHOOK_URL,
      allowed_updates: ["callback_query"],
      drop_pending_updates: false,
    });
  }

  const verified = await tg(env, "getWebhookInfo", {});
  const v = verified?.result || verified || {};
  const verifiedAllowed = Array.isArray(v.allowed_updates) ? v.allowed_updates : [];
  const urlMatches = String(v.url || "") === EXPECTED_TELEGRAM_WEBHOOK_URL;
  const callbackQueryAllowed = verifiedAllowed.includes("callback_query");
  return {
    ok: urlMatches && callbackQueryAllowed,
    status: urlMatches && callbackQueryAllowed ? "TELEGRAM_WEBHOOK_OK" : "TELEGRAM_WEBHOOK_MISMATCH",
    urlMatches,
    callbackQueryAllowed,
    noSecretValuesExposed: true,
  };
}

async function handleTelegramWebhook(request, env) {
  const u = await request.json().catch(() => null);
  const q = u?.callback_query;
  if (!q) return new Response("ok");
  if (String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return new Response("ok");

  const [action, id] = String(q.data || "").split(":");
  const c = creds(env);

  if ((action === "PREP" || action === "CONFIRM") && c.credentialMode !== "LIVE") {
    await tg(env, "answerCallbackQuery", {
      callback_query_id: q.id,
      text: "Live Binance credentials are not configured — no real trade can execute",
      show_alert: true,
    });
    return new Response("ok");
  }

  const s = await getState(env, `live-signal:${id}`);
  if (action === "PREP" && s) {
    if (!executorConfigured(env)) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Execution provider is not ready — no trade prepared", show_alert: true });
      return new Response("ok");
    }
    const requested = Number(s.confirmedQuoteUSDT || s.recommendedUSDT || 0);
    if (!Number.isFinite(requested) || requested < MIN_ORDER_USDT) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Order size is below the safe minimum", show_alert: true });
      return new Response("ok");
    }
    const rec = Math.floor(Math.min(requested, 5.5) * 100) / 100;
    const p = { ...s, confirmedQuoteUSDT: rec, prepareExpiresAt: Date.now() + PREPARE_TTL_SEC * 1000 };
    await putState(env, `prepared:${id}`, p, PREPARE_TTL_SEC);
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Trade prepared — no purchase yet" });
    await tg(env, "sendMessage", {
      chat_id: String(env.TELEGRAM_CHAT_ID),
      text: `🧾 TRADE READY — ${p.symbol}\n💵 ${fmt(rec)} USDT\n💲 Entry ${fmt(p.entry)}\n🎯 TP ${fmt(p.target)}\n🛑 SL ${fmt(p.stop)}\n\n⚠️ CONFIRM BUY ينفذ شراء حقيقي.`,
      reply_markup: { inline_keyboard: [[{ text: `✅ CONFIRM BUY ${fmt(rec)} USDT`, callback_data: `CONFIRM:${id}` }], [{ text: "❌ CANCEL", callback_data: `CANCEL:${id}` }]] },
    });
    return new Response("ok");
  }

  if (action === "CANCEL") {
    await putState(env, `prepared:${id}`, null, 1);
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Cancelled" });
    return new Response("ok");
  }

  if (action === "CONFIRM") {
    const p = await getState(env, `prepared:${id}`);
    if (!p || Date.now() > Number(p.prepareExpiresAt || 0)) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Expired", show_alert: true });
      return new Response("ok");
    }

    const liveEnabled = String(env.LIVE_EXECUTION_ENABLED || "").toLowerCase() === "true";
    if (!liveEnabled) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Manual live execution is not armed yet — no order sent", show_alert: true });
      return new Response("ok");
    }
    const autonomousEnabled = String(env.AUTONOMOUS_ENABLED || "").toLowerCase() === "true";
    const e2eArmed = String(env.E2E_ARMED || "").toLowerCase() === "true";
    const priorE2E = await getState(env, "cutover:e2e:result");
    const e2eComplete = priorE2E?.ok === true && priorE2E?.manual === true;
    if (!e2eComplete && (!e2eArmed || autonomousEnabled)) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "First live E2E is not armed for manual confirmation — no order sent", show_alert: true });
      return new Response("ok");
    }

    const claimed = await claimState(env, `execution-lock:${id}`, { claimedAt: Date.now(), symbol: p.symbol }, SIGNAL_TTL_SEC);
    if (!claimed) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Already confirmed — duplicate blocked", show_alert: true });
      return new Response("ok");
    }

    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Executing confirmed Spot BUY…" });
    try {
      const r = await executeConfirmedBuy(env, p);
      const priorE2E = await getState(env, "cutover:e2e:result");
      if (!(priorE2E?.ok === true && priorE2E?.manual === true) && String(env.E2E_ARMED || "").toLowerCase() === "true") {
        await putState(env, "cutover:e2e:result", {
          ok: r.ocoPlaced === true,
          manual: true,
          automaticExecution: false,
          at: Date.now(),
          status: r.status,
          symbol: p.symbol,
          quoteUSDT: r.quoteUSDT,
          buyOrderId: r.buyOrderId || null,
          ocoOrderListId: r.ocoOrderListId || null,
          emergencyOrderId: r.emergencyOrderId || null,
          clientIds: r.clientIds || null,
          executionRoute: r.executionRoute || executionProvider(env),
        }, 30 * 24 * 60 * 60);
      }
      await putState(env, `execution-result:${id}`, {
        ok: true,
        at: Date.now(),
        status: r.status,
        symbol: p.symbol,
        quoteUSDT: r.quoteUSDT,
        ocoPlaced: r.ocoPlaced,
        emergencyClosed: r.emergencyClosed,
      }, 86400);
      await putState(env, `prepared:${id}`, null, 1);
      const resultText = r.ocoPlaced
        ? `✅ BUY تم — ${p.symbol}\n💵 ${fmt(r.quoteUSDT)} USDT\n💲 ${fmt(r.avg)}\n✅ TP ${fmt(r.tp)} | SL ${fmt(r.stop)}`
        : r.emergencyClosed
          ? `⚠️ ${p.symbol}: الشراء اتنفذ لكن حماية OCO فشلت، فالبوت قفل المركز فورًا Market كإجراء طوارئ. مفيش مركز مقصود يفضل مفتوح من العملية دي.`
          : `🚨 CRITICAL — ${p.symbol}: الشراء اتنفذ، وحماية OCO فشلت، ومحاولة الإغلاق الطارئ فشلت. راجعي Binance فورًا.`;
      await tg(env, "sendMessage", {
        chat_id: String(env.TELEGRAM_CHAT_ID),
        text: resultText,
      });
    } catch (e) {
      const error = String(e?.message || e);
      await putState(env, `execution-result:${id}`, { ok: false, at: Date.now(), error }, 86400);
      if (String(env.E2E_ARMED || "").toLowerCase() === "true") {
        const priorE2E = await getState(env, "cutover:e2e:result");
        if (!(priorE2E?.ok === true && priorE2E?.manual === true)) {
          await putState(env, "cutover:e2e:result", {
            ok: false,
            manual: true,
            automaticExecution: false,
            at: Date.now(),
            status: "MANUAL_E2E_FAILED",
            symbol: p.symbol,
            error: error.slice(0, 180),
            executionRoute: executionProvider(env),
          }, 30 * 24 * 60 * 60);
        }
      }
      await tg(env, "sendMessage", { chat_id: String(env.TELEGRAM_CHAT_ID), text: `❌ BUY failed: ${error.slice(0, 250)}` });
    }
    return new Response("ok");
  }

  return new Response("ok");
}

async function notifyExecutionReadinessTransition(env, balance) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  const c=creds(env);
  const lastError=balance ? null : await getState(env,"binance:balance:error");
  const ready=Boolean(
    c.credentialMode==="LIVE" &&
    c.route===executionRoute(env) &&
    balance?.ok &&
    balance?.canTrade &&
    balance?.accountSafetyOk === true
  );
  const blocker=ready ? null : (balance?.accountSafetyReasons?.[0] || safeRelayDiagnostic(lastError?.error));
  const previous=await getState(env,"live-readiness:transition-state");
  const current={
    ready,
    blocker,
    checkedAt:Date.now(),
  };

  // First observation only establishes the baseline; never spam on deploy/restart.
  if (!previous || typeof previous.ready!=="boolean") {
    await putState(env,"live-readiness:transition-state",current,30*24*60*60);
    return;
  }

  if (previous.ready===ready && String(previous.blocker||"")===String(blocker||"")) {
    await putState(env,"live-readiness:transition-state",current,30*24*60*60);
    return;
  }

  await putState(env,"live-readiness:transition-state",current,30*24*60*60);

  if (ready && previous.ready!==true) {
    await tg(env,"sendMessage",{
      chat_id:String(env.TELEGRAM_CHAT_ID),
      text:"✅ LIVE EXECUTION READY\nBinance authentication on Vercel is valid now.\nالشراء مازال محتاج CONFIRM منك على Telegram — مفيش Auto Buy.",
    });
    return;
  }

  if (!ready && previous.ready===true) {
    await tg(env,"sendMessage",{
      chat_id:String(env.TELEGRAM_CHAT_ID),
      text:`🚨 LIVE EXECUTION BLOCKED\nReason: ${String(blocker||"UNKNOWN").slice(0,80)}\nمفيش أوامر جديدة هتتنفذ لحد ما الجاهزية ترجع.`,
    });
  }
}


export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") return handleFastSignalIngest(request, env);
    if (url.pathname === "/telegram-webhook" && request.method === "POST") return handleTelegramWebhook(request, env);
    if (url.pathname === "/telegram-webhook-check" && request.method === "POST") {
      try {
        return Response.json(await ensureTelegramWebhook(env), { headers: { "cache-control": "no-store" } });
      } catch (e) {
        return Response.json({
          ok:false,
          status:"TELEGRAM_WEBHOOK_CHECK_FAILED",
          reason:String(e?.message || e).slice(0,120),
          noSecretValuesExposed:true,
        }, { status:502, headers:{ "cache-control":"no-store" } });
      }
    }

    if (url.pathname === "/runtime-check") {
      const c = creds(env);
      const configured = executorConfigured(env);
      const provider = executionProvider(env);
      return Response.json({
        ok: true,
        telegramConfigured: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
        credentialMode: c.credentialMode,
        executionProvider: provider,
        executionRoute: c.route,
        binanceCredentialOwner: provider === "SUPABASE_V2" ? "CLOUDFLARE_SECRET" : "MAKE_CONNECTION",
        cloudflareBinanceCredentialsRequired: provider === "SUPABASE_V2",
        atomicConfirmClaim: true,
        fastSignalIngest: true,
        oneTapConfirm: true,
        autoBuy: false,
        executionGatewayPrepared: true,
        executionGatewayActive: configured,
        executorConfigured: configured,
        oldVercelFallback: false,
        noSecretValuesExposed: true,
      });
    }

    if (url.pathname === "/live-readiness") {
      const c = creds(env);
      const provider = executionProvider(env);
      const telegramConfigured = Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID);
      const reconciliation = await getState(env, "ops:reconciliation:last");
      const opsState = await getState(env, "ops:state");
      const configured = executorConfigured(env);
      const liveExecutionEnabled = String(env.LIVE_EXECUTION_ENABLED || "").toLowerCase() === "true";
      const autonomousEnabled = String(env.AUTONOMOUS_ENABLED || "").toLowerCase() === "true";
      const e2eArmed = String(env.E2E_ARMED || "").toLowerCase() === "true";
      const operational = await executionOperational(env);
      const infrastructureReady = c.route === executionRoute(env)
        && telegramConfigured
        && configured
        && operational.ok === true
        && reconciliation?.ok === true
        && opsState?.state === "HEALTHY";
      const executionReady = infrastructureReady && liveExecutionEnabled && e2eArmed && !autonomousEnabled;
      const blocker = !configured
        ? "EXECUTOR_INCOMPLETE"
        : !infrastructureReady
          ? "OPERATIONAL_GO_REQUIRED"
          : !liveExecutionEnabled
            ? "LIVE_POLICY_DISABLED"
            : autonomousEnabled
              ? "AUTONOMOUS_MUST_BE_OFF_FOR_MANUAL_E2E"
              : !e2eArmed
                ? "MANUAL_E2E_NOT_ARMED"
                : null;
      return Response.json({
        ok: true,
        status: executionReady ? "MANUAL_LIVE_E2E_ARMED" : "LIVE_EXECUTION_BLOCKED",
        infrastructureReady,
        executionReady,
        blocker,
        credentialMode: c.credentialMode,
        executionProvider: provider,
        executionRoute: c.route,
        telegramConfigured,
        scannerRunning: true,
        userConfirmationRequired: true,
        autoBuy: false,
        liveExecutionEnabled,
        e2eArmed,
        autonomousExecution: autonomousEnabled,
        manualOnly: true,
        railwayDependency: false,
        binanceCredentialOwner: provider === "SUPABASE_V2" ? "CLOUDFLARE_SECRET" : "MAKE_CONNECTION",
        cloudflareBinanceCredentialsRequired: provider === "SUPABASE_V2",
        maxRiskUSDT: MAX_RISK_USDT,
        maxBuyUSDT: 5.5,
        reconciliationOk: reconciliation?.ok === true,
        supervisorState: opsState?.state || null,
        executorConfigured: configured,
        operational,
        oldVercelFallback: false,
        noBalanceValuesExposed: true,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }

    if (url.pathname === "/balance-refresh") {
      const reconciliation = await getState(env, "ops:reconciliation:last");
      const configured = executorConfigured(env);
      const provider = executionProvider(env);
      const heartbeat = await executionReadOnlyHeartbeat(env);
      const canTrade = heartbeat?.body?.canTrade === true;
      const heartbeatOk = heartbeat?.transportOk === true;
      return Response.json({
        ok: heartbeatOk && reconciliation?.ok === true,
        canTrade,
        accountSafetyOk: heartbeatOk && canTrade,
        accountSafetyReasons: heartbeatOk && canTrade
          ? []
          : [configured ? "EXECUTION_READONLY_PREFLIGHT_FAILED" : "EXECUTOR_INCOMPLETE"],
        credentialMode: creds(env).credentialMode,
        autoBuy: false,
        executionProvider: provider,
        executionRoute: executionRoute(env),
        diagnosticCode: provider === "SUPABASE_V2" ? "SUPABASE_READ_ONLY_WATCHDOG" : "MAKE_READ_ONLY_WATCHDOG",
        oldVercelFallback: false,
        noBalanceValuesExposed: true,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }

    return baseWorker.fetch(request, env, ctx);
  },


  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      if (baseWorker.scheduled) await baseWorker.scheduled(event, env, ctx);
      // Manual E2E preparation is non-financial: it only sends a Telegram confirmation prompt.
      // No order can be submitted from cron; execution still requires the user's Telegram callback.
      await prepareManualE2EPrompt(env).catch(() => {});
    })());
  }
};
