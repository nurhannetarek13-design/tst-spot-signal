import stableWorker, { SignalState } from "./buy-gateway-stable.js";
import edgeWorker from "./edge-worker.js";
export { SignalState };

const VERCEL_SIGNED_RELAY_URL = "https://tst-spot-signal.vercel.app/api/binance-signed-relay";
const DEMO_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_DEMO_READONLY";
const EXPECTED_TELEGRAM_WEBHOOK_URL = "https://tst-spot-signal.nurhanne-tarek13.workers.dev/telegram-webhook";
const MAKE_RELAY_URL = "https://freqtrade-production-43ed.up.railway.app/make-exec-relay";
const MAKE_ROUTE = "CLOUDFLARE_SIGNED_RAILWAY_MAKE_BINANCE";
const SIGNAL_TTL_SEC = 10 * 60;
const PREPARE_TTL_SEC = 5 * 60;
const MIN_ORDER_USDT = 5;

function mode(env) {
  const liveKey = env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "";
  const liveSecret = env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || "";
  if (liveKey && liveSecret) return { credentialMode: "LIVE", route: "CLOUDFLARE_SIGNED_VERCEL_TRANSPORT" };
  const demoKey = env.BINANCE_DEMO_API_KEY || "";
  const demoSecret = env.BINANCE_DEMO_SECRET_KEY || "";
  if (demoKey && demoSecret) {
    return {
      credentialMode: "DEMO",
      route: DEMO_ROUTE,
      key: String(demoKey).trim(),
      secret: String(demoSecret).trim(),
    };
  }
  return { credentialMode: "MISSING", route: "BINANCE_CREDENTIALS_MISSING" };
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

async function telegramApi(env, method, payload = null) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_TOKEN_MISSING");
  const init = payload == null
    ? { method: "GET", headers: { "cache-control": "no-store" } }
    : {
        method: "POST",
        headers: { "content-type": "application/json", "cache-control": "no-store" },
        body: JSON.stringify(payload),
      };
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, init);
  const row = await r.json().catch(() => ({ ok: false, description: "NON_JSON_TELEGRAM_RESPONSE" }));
  if (!r.ok || row.ok !== true) throw new Error(`TELEGRAM_${method.toUpperCase()}_FAILED`);
  return row.result;
}

function fmt(v) {
  return Number(v || 0).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 });
}

function safeWebhookState(info) {
  const allowed = Array.isArray(info?.allowed_updates) ? info.allowed_updates : [];
  return {
    urlMatches: String(info?.url || "") === EXPECTED_TELEGRAM_WEBHOOK_URL,
    callbackQueryAllowed: allowed.includes("callback_query"),
    pendingUpdateCount: Number(info?.pending_update_count || 0),
    hasLastError: Boolean(info?.last_error_message),
    lastErrorDate: Number(info?.last_error_date || 0) || null,
  };
}

async function ensureTelegramWebhook(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    return { ok: false, status: "TELEGRAM_NOT_CONFIGURED", noSecretValuesExposed: true };
  }

  let info = await telegramApi(env, "getWebhookInfo");
  let state = safeWebhookState(info);
  let changed = false;
  if (!state.urlMatches || !state.callbackQueryAllowed) {
    await telegramApi(env, "setWebhook", {
      url: EXPECTED_TELEGRAM_WEBHOOK_URL,
      allowed_updates: ["callback_query"],
      drop_pending_updates: false,
    });
    changed = true;
    info = await telegramApi(env, "getWebhookInfo");
    state = safeWebhookState(info);
  }

  return {
    ok: state.urlMatches && state.callbackQueryAllowed,
    status: state.urlMatches && state.callbackQueryAllowed ? "TELEGRAM_WEBHOOK_OK" : "TELEGRAM_WEBHOOK_MISMATCH",
    changed,
    ...state,
    noSecretValuesExposed: true,
  };
}

async function handleFastSignalIngestMake(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    return Response.json({ ok: false, status: "TELEGRAM_NOT_CONFIGURED", autoBuy: false }, { status: 503 });
  }
  const raw = await request.text();
  const ts = String(request.headers.get("x-fast-timestamp") || "");
  const supplied = String(request.headers.get("x-fast-signature") || "").toLowerCase();
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) {
    return Response.json({ ok: false, status: "STALE_INGEST" }, { status: 401 });
  }
  const expected = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`);
  if (!/^[a-f0-9]{64}$/.test(supplied) || supplied !== expected) {
    return Response.json({ ok: false, status: "BAD_INGEST_SIGNATURE" }, { status: 401 });
  }

  let body = {};
  try { body = JSON.parse(raw || "{}"); } catch {
    return Response.json({ ok: false, status: "BAD_JSON" }, { status: 400 });
  }
  const symbol = String(body.symbol || "").toUpperCase();
  const entry = Number(body.entry);
  const stop = Number(body.stop);
  const target = Number(body.target);
  const requested = Math.floor(Number(body.stakeUSDT) * 100) / 100;
  const score = Number(body.score);
  if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) return Response.json({ ok: false, status: "BAD_SYMBOL" }, { status: 400 });
  if (![entry, stop, target].every(Number.isFinite) || !(stop < entry && target > entry)) {
    return Response.json({ ok: false, status: "BAD_LEVELS" }, { status: 400 });
  }
  if (!Number.isFinite(requested) || requested < MIN_ORDER_USDT || requested > 10) {
    return Response.json({ ok: false, status: "BAD_STAKE" }, { status: 400 });
  }

  if (body.dryRun === true) {
    return Response.json({
      ok: true,
      status: "FAST_SIGNAL_DRYRUN_OK",
      canTrade: true,
      credentialMode: "MAKE_VERIFIED",
      executionRoute: MAKE_ROUTE,
      recommendedUSDT: requested,
      autoBuy: false,
      userConfirmationRequired: true,
    });
  }

  const rawId = String(body.id || `${symbol}-${Date.now()}`);
  const id = rawId.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) || `${Date.now()}`;
  const now = Date.now();
  const signal = {
    id,
    symbol,
    entry,
    stop,
    target,
    strategy: String(body.strategy || "FAST30_60").slice(0, 100),
    score: Number.isFinite(score) ? score : null,
    createdAt: now,
    expiresAt: now + SIGNAL_TTL_SEC * 1000,
    recommendedUSDT: requested,
    confirmedQuoteUSDT: requested,
    prepareExpiresAt: now + PREPARE_TTL_SEC * 1000,
  };
  await putState(env, `live-signal:${id}`, signal, SIGNAL_TTL_SEC);
  await putState(env, `prepared:${id}`, signal, PREPARE_TTL_SEC);
  await telegramApi(env, "sendMessage", {
    chat_id: String(env.TELEGRAM_CHAT_ID),
    text: `🚨 CONFIRMED BUY — ${symbol} — SPOT\n💵 ${fmt(requested)} USDT\n💲 Entry ref ${fmt(entry)}\n🎯 TP ${fmt(target)}\n🛑 SL ${fmt(stop)}\n⭐ Score ${Number.isFinite(score) ? score : "—"}/100\n\n⚡ CONFIRM BUY = Market Buy على Binance عن طريق Make ثم TP/SL OCO تلقائيًا.`,
    reply_markup: { inline_keyboard: [[{ text: `✅ CONFIRM BUY ${fmt(requested)} USDT`, callback_data: `CONFIRM:${id}` }], [{ text: "❌ CANCEL", callback_data: `CANCEL:${id}` }]] },
  });
  return Response.json({
    ok: true,
    status: "FAST_SIGNAL_READY",
    id,
    symbol,
    recommendedUSDT: requested,
    canTrade: true,
    credentialMode: "MAKE_VERIFIED",
    executionRoute: MAKE_ROUTE,
    autoBuy: false,
    userConfirmationRequired: true,
  });
}

async function executeViaMakeRelay(env, id, p) {
  const payload = {
    signal_id: id,
    action: "BUY",
    symbol: String(p.symbol || "").toUpperCase(),
    quote_amount_usdt: Number(p.confirmedQuoteUSDT || p.recommendedUSDT),
    take_profit_price: Number(p.target),
    stop_loss_price: Number(p.stop),
    confirmed: true,
    dry_run: false,
    timestamp: Math.floor(Date.now() / 1000),
  };
  const raw = JSON.stringify(payload);
  const ts = String(Date.now());
  const signature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`);
  const r = await fetch(MAKE_RELAY_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-make-relay-timestamp": ts,
      "x-make-relay-signature": signature,
      "cache-control": "no-store",
    },
    body: raw,
  });
  const text = await r.text();
  let row = {};
  try { row = JSON.parse(text || "{}"); } catch { row = { ok: false, status: `NON_JSON_${r.status}` }; }
  if (!r.ok || row.ok !== true) throw new Error(String(row.status || `MAKE_HTTP_${r.status}`));
  return row;
}

async function handleTelegramConfirmViaMake(request, env) {
  const u = await request.json().catch(() => null);
  const q = u?.callback_query;
  if (!q) return null;
  if (String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return new Response("ok");
  const [action, id] = String(q.data || "").split(":");
  if (action !== "CONFIRM") return null;

  const p = await getState(env, `prepared:${id}`);
  if (!p || Date.now() > Number(p.prepareExpiresAt || 0)) {
    await telegramApi(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Expired", show_alert: true });
    return new Response("ok");
  }
  const claimed = await claimState(env, `execution-lock:${id}`, { claimedAt: Date.now(), symbol: p.symbol, route: MAKE_ROUTE }, SIGNAL_TTL_SEC);
  if (!claimed) {
    await telegramApi(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Already confirmed — duplicate blocked", show_alert: true });
    return new Response("ok");
  }

  await telegramApi(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Executing confirmed Spot BUY via Make…" });
  try {
    const row = await executeViaMakeRelay(env, id, p);
    await putState(env, `execution-result:${id}`, { ok: true, at: Date.now(), route: MAKE_ROUTE, result: row }, 86400);
    await putState(env, `prepared:${id}`, null, 1);
    await telegramApi(env, "sendMessage", {
      chat_id: String(env.TELEGRAM_CHAT_ID),
      text: `✅ BUY + OCO تم — ${p.symbol}\n💵 ${fmt(row.quote_spent || p.confirmedQuoteUSDT || p.recommendedUSDT)} USDT\n📦 Qty ${fmt(row.executed_qty)}\n🛡️ TP/SL protected\nOrder ${row.order_id || "—"}`,
    });
  } catch (e) {
    await putState(env, `execution-result:${id}`, { ok: false, at: Date.now(), route: MAKE_ROUTE, error: String(e?.message || e) }, 86400);
    await telegramApi(env, "sendMessage", {
      chat_id: String(env.TELEGRAM_CHAT_ID),
      text: `⚠️ EXECUTION STATUS UNCERTAIN — ${p.symbol}\n${String(e?.message || e).slice(0, 180)}\nراجعي Binance Spot + Open Orders فورًا قبل أي إعادة محاولة.`,
    });
  }
  return new Response("ok");
}

async function demoAccountViaVercel(env) {
  const c = mode(env);
  if (c.credentialMode !== "DEMO" || !c.key || !c.secret) throw new Error("DEMO_CREDENTIALS_MISSING");
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("RELAY_SECRET_UNAVAILABLE");

  const qs = new URLSearchParams({ recvWindow: "5000", timestamp: String(Date.now()) }).toString();
  const binanceSignature = await hmacHex(c.secret, qs);
  const body = JSON.stringify({
    method: "GET",
    path: "/api/v3/account",
    apiKey: c.key,
    network: "demo",
    query: `${qs}&signature=${binanceSignature}`,
  });
  const ts = String(Date.now());
  const relaySignature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);

  const r = await fetch(VERCEL_SIGNED_RELAY_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-executor-timestamp": ts,
      "x-executor-signature": relaySignature,
      "cache-control": "no-store",
    },
    body,
  });
  const text = await r.text();
  let row = {};
  try { row = JSON.parse(text || "{}"); } catch {
    const preview = String(text || "").replace(/[A-Za-z0-9_-]{20,}/g, "[redacted]").slice(0, 240);
    throw new Error(`BINANCE_DEMO_RELAY_BAD_RESPONSE: http=${r.status} body=${preview}`);
  }
  if (!r.ok || row.ok !== true) {
    const detail = row?.upstream?.code != null
      ? `${row.upstream.code} ${row.upstream.msg || ""}`
      : (row.reason || row.status || r.status);
    throw new Error(`BINANCE_DEMO_RELAY_ERROR: ${detail}`);
  }
  return row.data;
}

async function refreshDemoBalance(env) {
  try {
    const account = await demoAccountViaVercel(env);
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
      source: DEMO_ROUTE,
      network: "demo",
      credentialMode: "DEMO",
      checkedAt: Date.now(),
    };
    await putState(env, "binance:balance:last", balance, 7200);
    await putState(env, "binance:balance:error", null, 60);
    return balance;
  } catch (e) {
    await putState(env, "binance:balance:error", {
      at: Date.now(),
      credentialMode: "DEMO",
      route: DEMO_ROUTE,
      error: String(e?.message || e),
    }, 1800);
    return null;
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const c = mode(env);

    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") {
      return handleFastSignalIngestMake(request, env);
    }

    if (url.pathname === "/telegram-webhook" && request.method === "POST") {
      const handled = await handleTelegramConfirmViaMake(request.clone(), env);
      if (handled) return handled;
      return stableWorker.fetch(request, env, ctx);
    }

    if (url.pathname === "/telegram-webhook-check" && request.method === "POST") {
      try {
        return Response.json(await ensureTelegramWebhook(env));
      } catch (e) {
        return Response.json({
          ok: false,
          status: "TELEGRAM_WEBHOOK_CHECK_FAILED",
          reason: String(e?.message || e).slice(0, 120),
          noSecretValuesExposed: true,
        }, { status: 502 });
      }
    }

    if (url.pathname === "/make-runtime-check") {
      return Response.json({
        ok: true,
        executionRoute: MAKE_ROUTE,
        fastSignalIngest: true,
        oneTapConfirm: true,
        userConfirmationRequired: true,
        atomicConfirmClaim: true,
        autoBuy: false,
        telegramConfigured: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
        noSecretValuesExposed: true,
      });
    }

    if (c.credentialMode === "DEMO" && url.pathname === "/runtime-check") {
      return Response.json({
        ok: true,
        hasApiKey: Boolean(c.key),
        hasApiSecret: Boolean(c.secret),
        keyAlias: "BINANCE_DEMO_API_KEY",
        secretAlias: "BINANCE_DEMO_SECRET_KEY",
        telegramConfigured: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
        demoApiKeyBindingPresent: Object.prototype.hasOwnProperty.call(env, "BINANCE_DEMO_API_KEY"),
        demoApiKeyType: typeof env.BINANCE_DEMO_API_KEY,
        demoSecretBindingPresent: Object.prototype.hasOwnProperty.call(env, "BINANCE_DEMO_SECRET_KEY"),
        demoSecretType: typeof env.BINANCE_DEMO_SECRET_KEY,
        credentialMode: "DEMO",
        executionRoute: DEMO_ROUTE,
        atomicConfirmClaim: true,
        demoExecutionDisabled: true,
        autoBuy: false,
        noSecretValuesExposed: true,
      });
    }

    if (c.credentialMode === "DEMO" && url.pathname === "/balance-refresh") {
      const balance = await refreshDemoBalance(env);
      const error = balance ? null : await getState(env, "binance:balance:error");
      return Response.json({
        ok: Boolean(balance),
        balance,
        error,
        credentialMode: "DEMO",
        autoBuy: false,
        executionRoute: DEMO_ROUTE,
      });
    }

    return stableWorker.fetch(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    const c = mode(env);
    if (c.credentialMode === "DEMO") {
      ctx.waitUntil((async () => {
        if (edgeWorker.scheduled) await edgeWorker.scheduled(event, env, ctx);
        await refreshDemoBalance(env);
      })());
      return;
    }
    if (stableWorker.scheduled) return stableWorker.scheduled(event, env, ctx);
  },
};