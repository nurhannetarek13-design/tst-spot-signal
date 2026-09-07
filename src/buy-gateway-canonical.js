import stableWorker, { SignalState } from "./buy-gateway-stable.js";
import edgeWorker from "./edge-worker.js";
export { SignalState };

const VERCEL_SIGNED_RELAY_URL = "https://tst-spot-signal.vercel.app/api/binance-signed-relay";
const DEMO_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_DEMO_READONLY";

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
