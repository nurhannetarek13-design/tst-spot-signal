import baseWorker, { SignalState } from "./edge-worker.js";
export { SignalState };

const SIGNAL_TTL_SEC = 10 * 60;
const PREPARE_TTL_SEC = 5 * 60;
const MAX_SIGNAL_AGE_MS = 10 * 60 * 1000;
const MIN_ORDER_USDT = 5;
const MAX_BALANCE_FRACTION = 0.80;
const MAX_RISK_USDT = 0.20;
const VERCEL_SIGNED_RELAY_URL = "https://tst-spot-signal.vercel.app/api/binance-signed-relay";
const RAILWAY_DEMO_ACCOUNT_URL = "https://liquidation-collector-production.up.railway.app/signed-testnet-account";
const DEMO_ROUTE = "CLOUDFLARE_SIGNED_RAILWAY_TESTNET_READONLY";
const LIVE_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_TRANSPORT";

function creds(env) {
  const liveKey = env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "";
  const liveSecret = env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || "";
  if (liveKey && liveSecret) {
    return {
      key: String(liveKey).trim(),
      secret: String(liveSecret).trim(),
      network: "production",
      credentialMode: "LIVE",
      route: LIVE_ROUTE,
    };
  }

  const demoKey = env.BINANCE_DEMO_API_KEY || "";
  const demoSecret = env.BINANCE_DEMO_SECRET_KEY || "";
  if (demoKey && demoSecret) {
    return {
      key: String(demoKey).trim(),
      secret: String(demoSecret).trim(),
      network: "testnet",
      credentialMode: "DEMO",
      route: DEMO_ROUTE,
    };
  }

  return {
    key: "",
    secret: "",
    network: "none",
    credentialMode: "MISSING",
    route: "BINANCE_CREDENTIALS_MISSING",
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


async function handleFastSignalIngest(request, env) {
  const c = creds(env);
  if (c.credentialMode !== "LIVE") {
    return Response.json({ ok:false, status:"LIVE_CREDENTIALS_REQUIRED", credentialMode:c.credentialMode, autoBuy:false }, {status:503});
  }
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

  const b=await refreshBalance(env);
  if (!b?.ok || b.credentialMode !== "LIVE" || !b.canTrade) {
    return Response.json({ok:false,status:"ACCOUNT_PREFLIGHT_FAILED",autoBuy:false},{status:503});
  }
  const free=Number(b.usdt?.free||0);
  const rec=dynamicQuote(free,entry,stop,requested);
  if (rec < MIN_ORDER_USDT) return Response.json({ok:false,status:"SIZE_TOO_SMALL",autoBuy:false},{status:409});
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
  let size = Math.min(riskSized, balanceCap);
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

async function signedBinance(env, method, path, params = {}) {
  const c = creds(env);
  if (!c.key || !c.secret) throw new Error("BINANCE_CLOUDFLARE_KEYS_MISSING");

  const all = { ...params, recvWindow: 5000, timestamp: Date.now() };
  const qs = new URLSearchParams(Object.entries(all).map(([k, v]) => [k, String(v)])).toString();
  const binanceSignature = await hmacHex(c.secret, qs);
  const signedQuery = `${qs}&signature=${binanceSignature}`;

  if (c.credentialMode === "DEMO") {
    if (String(method).toUpperCase() !== "GET" || path !== "/api/v3/account") {
      throw new Error("DEMO_EXECUTION_DISABLED");
    }
    const r = await fetch(RAILWAY_DEMO_ACCOUNT_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "cache-control": "no-store" },
      body: JSON.stringify({ apiKey: c.key, query: signedQuery }),
    });
    const text = await r.text();
    let row = {};
    try { row = JSON.parse(text || "{}"); } catch { row = { ok: false, status: "NON_JSON_RAILWAY_RESPONSE" }; }
    if (!r.ok || row.ok !== true) {
      const detail = row?.upstream?.code != null
        ? `${row.upstream.code} ${row.upstream.msg || ""}`
        : (row.reason || row.status || r.status);
      throw new Error(`BINANCE_RAILWAY_DEMO_ERROR: ${detail}`);
    }
    return row.data;
  }

  if (c.credentialMode !== "LIVE") throw new Error("LIVE_CREDENTIALS_REQUIRED");
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("RELAY_SECRET_UNAVAILABLE");

  const body = JSON.stringify({
    method: String(method).toUpperCase(),
    path,
    apiKey: c.key,
    network: "production",
    query: signedQuery,
  });
  const ts = String(Date.now());
  const relaySignature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
  const r = await fetch(VERCEL_SIGNED_RELAY_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-executor-timestamp": ts,
      "x-executor-signature": relaySignature,
    },
    body,
  });
  const text = await r.text();
  let row = {};
  try { row = JSON.parse(text || "{}"); } catch {
    const preview = String(text || "").replace(/[A-Za-z0-9_-]{20,}/g, "[redacted]").slice(0, 240);
    row = {
      ok: false,
      status: "BAD_RELAY_RESPONSE",
      relayHttpStatus: r.status,
      relayContentType: r.headers.get("content-type") || "",
      relayBodyPreview: preview,
    };
  }
  if (!r.ok || row.ok !== true) {
    const detail = row?.upstream?.code != null
      ? `${row.upstream.code} ${row.upstream.msg || ""}`
      : row.status === "BAD_RELAY_RESPONSE"
        ? `BAD_RELAY_RESPONSE http=${row.relayHttpStatus} type=${row.relayContentType} body=${row.relayBodyPreview}`
        : (row.reason || row.status || r.status);
    throw new Error(`BINANCE_RELAY_ERROR: ${detail}`);
  }
  return row.data;
}

async function refreshBalance(env) {
  const c = creds(env);
  try {
    const account = await signedBinance(env, "GET", "/api/v3/account", {});
    const balances = (account.balances || [])
      .map((b) => ({ asset: b.asset, free: Number(b.free || 0), locked: Number(b.locked || 0) }))
      .filter((b) => b.free > 0 || b.locked > 0);
    const usdt = balances.find((b) => b.asset === "USDT") || { asset: "USDT", free: 0, locked: 0 };
    const balance = {
      ok: true,
      status: "ACCOUNT_BALANCE_OK",
      canTrade: Boolean(account.canTrade),
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

async function executeConfirmedBuy(env, s) {
  const c = creds(env);
  if (c.credentialMode !== "LIVE") throw new Error("LIVE_CREDENTIALS_REQUIRED");

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

  const account = await signedBinance(env, "GET", "/api/v3/account", {});
  if (!account.canTrade) throw new Error("ACCOUNT_CANNOT_TRADE");
  const freeUSDT = Number((account.balances || []).find((b) => b.asset === "USDT")?.free || 0);
  const quoteUSDT = dynamicQuote(freeUSDT, entryRef, stopRef, s.confirmedQuoteUSDT || s.recommendedUSDT);
  if (quoteUSDT < MIN_ORDER_USDT) throw new Error("SIZE_TOO_SMALL");
  if (freeUSDT < quoteUSDT) throw new Error("INSUFFICIENT_USDT");

  const buy = await signedBinance(env, "POST", "/api/v3/order", {
    symbol,
    side: "BUY",
    type: "MARKET",
    quoteOrderQty: quoteUSDT.toFixed(2),
    newOrderRespType: "FULL",
    newClientOrderId: `TSTU${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`,
  });
  const executedQty = Number(buy.executedQty || 0);
  const quoteQty = Number(buy.cummulativeQuoteQty || 0);
  if (!(executedQty > 0 && quoteQty > 0)) throw new Error("MARKET_BUY_ZERO_FILL");

  const avg = quoteQty / executedQty;
  const lot = market.filters.find((x) => x.filterType === "LOT_SIZE");
  const pf = market.filters.find((x) => x.filterType === "PRICE_FILTER");
  const step = Number(lot?.stepSize || "0.00000001");
  const tick = Number(pf?.tickSize || "0.00000001");
  const stop = roundTo(avg * (stopRef / entryRef), tick);
  const tp = roundTo(avg * (targetRef / entryRef), tick);
  const stopLimit = roundTo(stop * 0.997, tick);
  const sellQty = floorTo(executedQty * 0.999, step);

  let oco = null, ocoError = null;
  try {
    oco = await signedBinance(env, "POST", "/api/v3/orderList/oco", {
      symbol,
      side: "SELL",
      quantity: sellQty,
      aboveType: "LIMIT_MAKER",
      abovePrice: tp,
      belowType: "STOP_LOSS_LIMIT",
      belowStopPrice: stop,
      belowPrice: stopLimit,
      belowTimeInForce: "GTC",
    });
  } catch (e) {
    ocoError = String(e?.message || e);
  }

  return {
    ok: true,
    status: oco ? "BOUGHT_AND_PROTECTED" : "BOUGHT_PROTECTION_FAILED",
    symbol,
    quoteUSDT: quoteQty,
    recommendedUSDT: quoteUSDT,
    executedQty,
    avg,
    tp,
    stop,
    stopLimit,
    ocoPlaced: Boolean(oco),
    ocoError,
    autoBuy: false,
    userConfirmed: true,
  };
}

async function sendPromptForActive(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  const c = creds(env);
  if (c.credentialMode !== "LIVE") return;

  const active = (await getState(env, "paper:active")) || [];
  const b = (await getState(env, "binance:balance:last")) || await refreshBalance(env);
  const free = Number(b?.usdt?.free || 0);
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
    const rec = free > 0 ? dynamicQuote(free, s.entry, s.stop) : 0;
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
    const b = await refreshBalance(env);
    if (!b?.ok || b.credentialMode !== "LIVE") {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Live Binance balance unavailable — no trade prepared", show_alert: true });
      return new Response("ok");
    }
    const free = Number(b.usdt?.free || 0);
    const rec = dynamicQuote(free, Number(s.entry), Number(s.stop));
    if (rec < MIN_ORDER_USDT) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Free USDT is below the safe minimum", show_alert: true });
      return new Response("ok");
    }
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

    const claimed = await claimState(env, `execution-lock:${id}`, { claimedAt: Date.now(), symbol: p.symbol }, SIGNAL_TTL_SEC);
    if (!claimed) {
      await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Already confirmed — duplicate blocked", show_alert: true });
      return new Response("ok");
    }

    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Executing confirmed Spot BUY…" });
    try {
      const r = await executeConfirmedBuy(env, p);
      await putState(env, `execution-result:${id}`, {
        ok: true,
        at: Date.now(),
        status: r.status,
        symbol: p.symbol,
        quoteUSDT: r.quoteUSDT,
        ocoPlaced: r.ocoPlaced,
      }, 86400);
      await putState(env, `prepared:${id}`, null, 1);
      await tg(env, "sendMessage", {
        chat_id: String(env.TELEGRAM_CHAT_ID),
        text: `✅ BUY تم — ${p.symbol}\n💵 ${fmt(r.quoteUSDT)} USDT\n💲 ${fmt(r.avg)}\n${r.ocoPlaced ? `✅ TP ${fmt(r.tp)} | SL ${fmt(r.stop)}` : `⚠️ OCO failed: ${r.ocoError}`}`,
      });
    } catch (e) {
      await putState(env, `execution-result:${id}`, { ok: false, at: Date.now(), error: String(e?.message || e) }, 86400);
      await tg(env, "sendMessage", { chat_id: String(env.TELEGRAM_CHAT_ID), text: `❌ BUY failed: ${String(e?.message || e).slice(0, 250)}` });
    }
    return new Response("ok");
  }

  return new Response("ok");
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") return handleFastSignalIngest(request, env);
    if (url.pathname === "/telegram-webhook" && request.method === "POST") return handleTelegramWebhook(request, env);

    if (url.pathname === "/runtime-check") {
      const c = creds(env);
      return Response.json({
        ok: true,
        hasApiKey: Boolean(c.key),
        hasApiSecret: Boolean(c.secret),
        keyAlias: env.BINANCE_API_KEY ? "BINANCE_API_KEY" : env.BINANCE_KEY ? "BINANCE_KEY" : env.BINANCE_APIKEY ? "BINANCE_APIKEY" : env.BINANCE_DEMO_API_KEY ? "BINANCE_DEMO_API_KEY" : null,
        secretAlias: env.BINANCE_API_SECRET ? "BINANCE_API_SECRET" : env.BINANCE_SECRET ? "BINANCE_SECRET" : env.BINANCE_SECRET_KEY ? "BINANCE_SECRET_KEY" : env.BINANCE_DEMO_SECRET_KEY ? "BINANCE_DEMO_SECRET_KEY" : null,
        telegramConfigured: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
        demoApiKeyBindingPresent: Object.prototype.hasOwnProperty.call(env, "BINANCE_DEMO_API_KEY"),
        demoApiKeyType: typeof env.BINANCE_DEMO_API_KEY,
        demoSecretBindingPresent: Object.prototype.hasOwnProperty.call(env, "BINANCE_DEMO_SECRET_KEY"),
        demoSecretType: typeof env.BINANCE_DEMO_SECRET_KEY,
        credentialMode: c.credentialMode,
        executionRoute: c.route,
        atomicConfirmClaim: true,
        fastSignalIngest: true,
        oneTapConfirm: true,
        demoExecutionDisabled: c.credentialMode === "DEMO",
        autoBuy: false,
        noSecretValuesExposed: true,
      });
    }

    if (url.pathname === "/balance-refresh") {
      const c = creds(env);
      const balance = await refreshBalance(env);
      const error = balance ? null : await getState(env, "binance:balance:error");
      return Response.json({
        ok: Boolean(balance),
        balance,
        error,
        credentialMode: c.credentialMode,
        autoBuy: false,
        executionRoute: c.route,
      });
    }

    return baseWorker.fetch(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      if (baseWorker.scheduled) await baseWorker.scheduled(event, env, ctx);
      await refreshBalance(env);
      if (creds(env).credentialMode === "LIVE") await sendPromptForActive(env);
    })());
  },
};
