import worker, { SignalState } from "./buy-gateway-auth-wrapper.js";
import { deriveOpsState } from "./ops-state-machine.js";
import { verifyBridgeEnvelope, signBridgeEnvelope, rotateBridgeSecret } from "./bridge-auth.js";
import { readLivePolicy, evaluateGoNoGo } from "./live-cutover-policy.js";
import { MAKE_EXECUTION_ROUTE } from "./make-live-client.js";
import {
  executionReadOnlyHeartbeat,
  executionReadOnlyReconcile,
  executionProvider,
  executionRoute,
  executorConfigured,
  executionOwner,
  routeVersion,
  executionRouteIds,
} from "./live-execution-router.js";
import { supabaseRelayReplaySelftest } from "./supabase-live-client.js";
export { SignalState };

const STATE_TTL_SEC = 30 * 24 * 60 * 60;
const HEARTBEAT_STALE_MS = 20 * 60 * 1000;
const RECOVERY_HOLD_MS = 60 * 1000;
const WARMUP_MS = 2 * 60 * 1000;

function stub(env) {
  const id = env.STATE_COORDINATOR.idFromName("global");
  return env.STATE_COORDINATOR.get(id);
}
async function getState(env, key) {
  const r = await stub(env).fetch(`https://state/get?key=${encodeURIComponent(key)}`);
  return r.ok ? await r.json() : null;
}
async function putState(env, key, value, ttl = STATE_TTL_SEC) {
  await stub(env).fetch(`https://state/put?key=${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
}
async function claimState(env, key, value, ttl = STATE_TTL_SEC) {
  const r = await stub(env).fetch(`https://state/claim?key=${encodeURIComponent(key)}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
  return r.ok;
}
async function hmacHexRaw(secret, text) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(String(secret || "")),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function cloudflareBinanceCreds(env) {
  const key = env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "";
  const secret = env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || "";
  return { key: String(key || "").trim(), secret: String(secret || "").trim() };
}

function cloudflareBinanceCredentialPairs(env) {
  const keys = [
    ["BINANCE_API_KEY", env.BINANCE_API_KEY],
    ["BINANCE_KEY", env.BINANCE_KEY],
    ["BINANCE_APIKEY", env.BINANCE_APIKEY],
  ].filter(([,v]) => String(v || "").trim());
  const secrets = [
    ["BINANCE_API_SECRET", env.BINANCE_API_SECRET],
    ["BINANCE_SECRET", env.BINANCE_SECRET],
    ["BINANCE_SECRET_KEY", env.BINANCE_SECRET_KEY],
  ].filter(([,v]) => String(v || "").trim());
  const out = [];
  for (const [keyAlias,key] of keys) {
    for (const [secretAlias,secret] of secrets) {
      out.push({ keyAlias, secretAlias, key:String(key).trim(), secret:String(secret).trim() });
    }
  }
  return out;
}

async function cloudflareBinanceCredentialPairPreflight(env) {
  const pairs = cloudflareBinanceCredentialPairs(env);
  const attempts = [];
  for (const pair of pairs) {
    const qs = new URLSearchParams({
      omitZeroBalances: "true",
      recvWindow: "5000",
      timestamp: String(Date.now()),
    }).toString();
    const signature = await hmacHexRaw(pair.secret, qs);
    try {
      const r = await fetch("https://api-gcp.binance.com/api/v3/account?" + qs + "&signature=" + signature, {
        method: "GET",
        headers: {
          "X-MBX-APIKEY": pair.key,
          "accept": "application/json",
          "cache-control": "no-store",
        },
        signal: AbortSignal.timeout(12000),
      });
      const row = await r.json().catch(() => ({}));
      const ok = r.ok && !(Number(row?.code) < 0);
      attempts.push({
        keyAlias: pair.keyAlias,
        secretAlias: pair.secretAlias,
        ok,
        httpStatus: r.status,
        binanceCode: row?.code ?? null,
      });
      if (ok) {
        return {
          ok: true,
          status: "BINANCE_CREDENTIAL_PAIR_OK",
          selectedKeyAlias: pair.keyAlias,
          selectedSecretAlias: pair.secretAlias,
          canTrade: row?.canTrade === true,
          accountType: row?.accountType || null,
          attempts,
          financialAction: false,
          noBalanceValuesExposed: true,
          noSecretValuesExposed: true,
        };
      }
    } catch (e) {
      attempts.push({
        keyAlias: pair.keyAlias,
        secretAlias: pair.secretAlias,
        ok: false,
        transportError: String(e?.name || "Error"),
      });
    }
  }
  return {
    ok: false,
    status: "NO_VALID_BINANCE_CREDENTIAL_PAIR",
    attempts,
    financialAction: false,
    noBalanceValuesExposed: true,
    noSecretValuesExposed: true,
  };
}

async function cloudflareBinanceWsAccountPreflight(env) {
  const creds = cloudflareBinanceCreds(env);
  if (!creds.key || !creds.secret) {
    return { ok:false, status:"BINANCE_WS_CREDENTIALS_MISSING", financialAction:false };
  }

  const params = {
    apiKey: creds.key,
    recvWindow: 5000,
    timestamp: Date.now(),
  };
  const payload = Object.entries(params)
    .sort(([a],[b]) => a.localeCompare(b))
    .map(([key,value]) => `${key}=${String(value)}`)
    .join("&");
  const signature = await hmacHexRaw(creds.secret, payload);
  const requestId = crypto.randomUUID();
  const request = {
    id: requestId,
    method: "account.status",
    params: { ...params, signature },
  };

  return await new Promise((resolve) => {
    let settled = false;
    let opened = false;
    let ws;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws?.close(1000, "done"); } catch {}
      resolve(value);
    };

    try {
      ws = new WebSocket("wss://ws-api.binance.com:443/ws-api/v3");
    } catch (error) {
      return resolve({
        ok:false,
        status:"BINANCE_WS_CONSTRUCTOR_FAILED",
        reason:String(error?.message || error).slice(0,120),
        financialAction:false,
        noSecretValuesExposed:true,
      });
    }

    const timer = setTimeout(() => finish({
      ok:false,
      status:"BINANCE_WS_TIMEOUT",
      opened,
      financialAction:false,
      noSecretValuesExposed:true,
    }), 12_000);

    ws.addEventListener("open", () => {
      opened = true;
      try {
        ws.send(JSON.stringify(request));
      } catch (error) {
        finish({
          ok:false,
          status:"BINANCE_WS_SEND_FAILED",
          reason:String(error?.message || error).slice(0,120),
          financialAction:false,
          noSecretValuesExposed:true,
        });
      }
    });

    ws.addEventListener("message", (event) => {
      try {
        const message = JSON.parse(typeof event.data === "string" ? event.data : String(event.data));
        if (String(message?.id || "") !== requestId) return;
        const status = Number(message?.status || 0);
        const result = message?.result || {};
        finish({
          ok: status >= 200 && status < 300,
          status: status >= 200 && status < 300 ? "CLOUDFLARE_BINANCE_WS_READONLY_OK" : "CLOUDFLARE_BINANCE_WS_READONLY_FAILED",
          wsStatus: status,
          binanceCode: message?.error?.code ?? null,
          canTrade: result?.canTrade === true,
          accountType: result?.accountType || null,
          permissions: Array.isArray(result?.permissions) ? result.permissions : [],
          financialAction:false,
          noBalanceValuesExposed:true,
          noSecretValuesExposed:true,
        });
      } catch (error) {
        finish({
          ok:false,
          status:"BINANCE_WS_BAD_RESPONSE",
          reason:String(error?.message || error).slice(0,120),
          financialAction:false,
          noSecretValuesExposed:true,
        });
      }
    });

    ws.addEventListener("error", () => finish({
      ok:false,
      status: opened ? "BINANCE_WS_TRANSPORT_ERROR" : "BINANCE_WS_CONNECT_ERROR",
      opened,
      financialAction:false,
      noSecretValuesExposed:true,
    }));

    ws.addEventListener("close", (event) => {
      if (settled) return;
      finish({
        ok:false,
        status:"BINANCE_WS_CLOSED_BEFORE_RESPONSE",
        opened,
        closeCode:event?.code ?? null,
        closeReason:String(event?.reason || "").slice(0,120),
        financialAction:false,
        noSecretValuesExposed:true,
      });
    });
  });
}

async function cloudflareDirectBinanceReadOnlyPreflight(env) {
  const c = cloudflareBinanceCreds(env);
  if (!c.key || !c.secret) {
    return { ok: false, status: "CLOUDFLARE_BINANCE_CREDENTIALS_MISSING", financialAction: false };
  }
  const bases = [
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://api.binance.com",
  ];
  const attempts = [];
  for (const base of bases) {
    const qs = new URLSearchParams({
      omitZeroBalances: "true",
      recvWindow: "5000",
      timestamp: String(Date.now()),
    }).toString();
    const signature = await hmacHexRaw(c.secret, qs);
    try {
      const r = await fetch(`${base}/api/v3/account?${qs}&signature=${signature}`, {
        method: "GET",
        headers: {
          "X-MBX-APIKEY": c.key,
          "accept": "application/json",
          "cache-control": "no-store",
        },
        signal: AbortSignal.timeout(15_000),
      });
      const row = await r.json().catch(() => ({}));
      attempts.push({
        host: new URL(base).host,
        httpStatus: r.status,
        binanceCode: row?.code ?? null,
      });
      if (r.ok && !(Number(row?.code) < 0)) {
        return {
          ok: true,
          status: "CLOUDFLARE_DIRECT_BINANCE_READONLY_OK",
          httpStatus: r.status,
          binanceCode: row?.code ?? null,
          canTrade: row?.canTrade === true,
          accountType: row?.accountType || null,
          workingBaseHost: new URL(base).host,
          attempts,
          financialAction: false,
          noBalanceValuesExposed: true,
          noSecretValuesExposed: true,
        };
      }
      // A signed Binance application error proves reachability; no point
      // spraying alternate hosts for invalid credentials/permissions.
      if (Number(row?.code) < 0) break;
    } catch (e) {
      attempts.push({
        host: new URL(base).host,
        httpStatus: null,
        binanceCode: null,
        reason: String(e?.name || "FETCH_ERROR"),
      });
    }
  }
  return {
    ok: false,
    status: "CLOUDFLARE_DIRECT_BINANCE_READONLY_FAILED",
    canTrade: false,
    attempts,
    financialAction: false,
    noBalanceValuesExposed: true,
    noSecretValuesExposed: true,
  };
}
async function supabaseRelayReadOnlyPreflight(env) {
  const heartbeat = await executionReadOnlyHeartbeat(env);
  const reconciliation = await executionReadOnlyReconcile(env);
  const ok = heartbeat?.transportOk === true
    && heartbeat?.body?.canTrade === true
    && heartbeat?.body?.financialAction === false
    && reconciliation?.ok === true
    && reconciliation?.noUnknownOrders === true
    && reconciliation?.noUnprotectedPositions === true;
  return {
    ok,
    status: ok ? "SUPABASE_V2_READONLY_OK" : "SUPABASE_V2_READONLY_BLOCKED",
    canTrade: heartbeat?.body?.canTrade === true,
    accountType: heartbeat?.body?.accountType || reconciliation?.accountType || null,
    noUnknownOrders: reconciliation?.noUnknownOrders === true,
    noUnprotectedPositions: reconciliation?.noUnprotectedPositions === true,
    protectedOrderLists:Number(reconciliation?.protectedOrderLists || 0),
    openOrdersChecked:Number(reconciliation?.openOrdersChecked || 0),
    executionProvider:executionProvider(env),
    executionRoute:executionRoute(env),
    heartbeatStatus:String(heartbeat?.body?.status || ""),
    reconciliationStatus:String(reconciliation?.status || ""),
    diagnostics:heartbeat?.raw?.diagnostics || reconciliation?.diagnostics?.account || null,
    financialAction:false,
    noBalanceValuesExposed:true,
    noSecretValuesExposed:true,
    relay:"SUPABASE_V2_EXECUTION_CLIENT",
  };
}

async function vercelImmutableRelayReadOnlyPreflight(env) {
  const c = cloudflareBinanceCreds(env);
  if (!c.key || !c.secret || !env.TELEGRAM_BOT_TOKEN) {
    return { ok: false, status: "IMMUTABLE_RELAY_CREDENTIALS_MISSING", financialAction: false };
  }
  const qs = new URLSearchParams({
    omitZeroBalances: "true",
    recvWindow: "5000",
    timestamp: String(Date.now()),
  }).toString();
  const binanceSignature = await hmacHexRaw(c.secret, qs);
  const body = JSON.stringify({
    method: "GET",
    path: "/api/v3/account",
    apiKey: c.key,
    network: "production",
    query: `${qs}&signature=${binanceSignature}`,
  });
  const ts = String(Date.now());
  const relaySignature = await hmacHexRaw(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
  try {
    const r = await fetch(
      "https://tst-spot-signal-3vq1z6iic-nurhannetarek13-3290s-projects.vercel.app/api/binance-signed-relay",
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-executor-timestamp": ts,
          "x-executor-signature": relaySignature,
        },
        body,
        signal: AbortSignal.timeout(15_000),
      },
    );
    const row = await r.json().catch(() => ({}));
    return {
      ok: r.ok && row?.ok === true,
      status: row?.status || `HTTP_${r.status}`,
      httpStatus: r.status,
      canTrade: row?.data?.canTrade === true,
      accountType: row?.data?.accountType || null,
      financialAction: false,
      noBalanceValuesExposed: true,
      noSecretValuesExposed: true,
    };
  } catch (e) {
    return {
      ok: false,
      status: "IMMUTABLE_VERCEL_RELAY_UNREACHABLE",
      reason: String(e?.message || e).slice(0, 100),
      financialAction: false,
      noSecretValuesExposed: true,
    };
  }
}

async function vercelTransportV2ReadOnlyPreflight(env) {
  const c = cloudflareBinanceCreds(env);
  if (!c.key || !c.secret || !env.TELEGRAM_BOT_TOKEN) {
    return { ok: false, status: "FREE_TRANSPORT_CREDENTIALS_MISSING", financialAction: false };
  }
  const qs = new URLSearchParams({
    omitZeroBalances: "true",
    recvWindow: "5000",
    timestamp: String(Date.now()),
  }).toString();
  const binanceSignature = await hmacHexRaw(c.secret, qs);
  const body = JSON.stringify({
    method: "GET",
    path: "/api/v3/account",
    apiKey: c.key,
    network: "production",
    query: `${qs}&signature=${binanceSignature}`,
  });
  const ts = String(Date.now());
  const relaySignature = await hmacHexRaw(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
  try {
    const r = await fetch("https://tst-spot-signal.vercel.app/api/binance-transport-v2", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-executor-timestamp": ts,
        "x-executor-signature": relaySignature,
      },
      body,
      signal: AbortSignal.timeout(15_000),
    });
    const row = await r.json().catch(() => ({}));
    return {
      ok: r.ok && row?.ok === true && row?.status === "BINANCE_READONLY_RELAY_OK",
      status: row?.status || `HTTP_${r.status}`,
      httpStatus: r.status,
      canTrade: row?.data?.canTrade === true,
      accountType: row?.data?.accountType || null,
      financialAction: false,
      noBalanceValuesExposed: true,
      noSecretValuesExposed: true,
    };
  } catch (e) {
    return {
      ok: false,
      status: "VERCEL_TRANSPORT_V2_UNREACHABLE",
      reason: String(e?.message || e).slice(0, 100),
      financialAction: false,
      noSecretValuesExposed: true,
    };
  }
}

async function tg(env, text) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: String(env.TELEGRAM_CHAT_ID), text }),
  }).catch(() => {});
}
async function heartbeat(env, components, meta = {}) {
  const now = Date.now();
  const current = (await getState(env, "ops:heartbeats")) || {};
  for (const c of components) current[String(c)] = { at: now, ...meta };
  await putState(env, "ops:heartbeats", current);
  return current;
}
async function computeState(env) {
  const now = Date.now();
  const hb = (await getState(env, "ops:heartbeats")) || {};
  const previous = (await getState(env, "ops:state")) || { state: "WARMING_UP", since: now };
  const reconciliation = (await getState(env, "ops:reconciliation:last")) || null;
  const scheduler = (await getState(env, "ops:scheduler:last")) || null;
  const next = deriveOpsState({
    now,
    heartbeats: hb,
    previous,
    reconciliation,
    scheduler,
    heartbeatStaleMs: HEARTBEAT_STALE_MS,
    recoveryHoldMs: RECOVERY_HOLD_MS,
    warmupMs: WARMUP_MS,
  });
  await putState(env, "ops:state", next);
  return next;
}
async function alertTransition(env, next) {
  const last = await getState(env, "ops:last-alert");
  const changed = !last || last.state !== next.state || last.reason !== next.reason;
  if (!changed) return;
  await putState(env, "ops:last-alert", { state: next.state, reason: next.reason, at: Date.now() });
  if (["DEGRADED", "PROTECTION_ONLY"].includes(next.state)) {
    await tg(env, `🚨 24/7 OPS — ${next.state}\nReason: ${next.reason || "UNKNOWN"}\nNew live entries remain disabled.`);
  } else if (next.state === "HEALTHY" && last && ["DEGRADED", "PROTECTION_ONLY"].includes(last.state)) {
    await tg(env, "✅ 24/7 OPS RECOVERED\nHealth, reconciliation and warmup recovered. Live entries are still disabled.");
  }
}
async function recordReconciliation(env, body) {
  const row = {
    ok: body?.ok === true,
    reason: body?.ok === true ? null : String(body?.reason || "RECONCILIATION_FAILED").slice(0, 120),
    openOrdersChecked: Number(body?.open_orders_checked || 0),
    protectedOrdersChecked: Number(body?.protected_orders_checked || 0),
    source: String(body?.source || "MAKE_BINANCE_READONLY").slice(0, 80),
    at: Date.now(),
  };
  await putState(env, "ops:reconciliation:last", row);
  await heartbeat(env, ["reconciler", "protection"], { source: row.source });
  return row;
}
function bridgeRouteId(body = {}) {
  const action = String(body?.action || "").toUpperCase();
  if (action === "BUY") return MAKE_EXECUTION_ROUTE.buy.id;
  if (action === "OCO") return MAKE_EXECUTION_ROUTE.oco.id;
  return "NON_FINANCIAL";
}

async function recordBridgeRouteAudit(env, routeId, status) {
  if (!["BUY_V2", "OCO_V2"].includes(String(routeId))) return;
  const key = `bridge:route:${routeId}`;
  const current = (await getState(env, key)) || {
    routeId,
    routeVersion: MAKE_EXECUTION_ROUTE.version,
    acceptedAt: 0,
    replayBlockedAt: 0,
  };
  const now = Date.now();
  const next = {
    routeId,
    routeVersion: MAKE_EXECUTION_ROUTE.version,
    acceptedAt: status === "BRIDGE_AUTH_OK" ? now : Number(current.acceptedAt || 0),
    replayBlockedAt: status === "REPLAY_BLOCKED" ? now : Number(current.replayBlockedAt || 0),
    lastStatus: String(status || "UNKNOWN").slice(0, 80),
    updatedAt: now,
  };
  await putState(env, key, next);
}

async function dispatchSignedEnvelope(url, envelope, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(envelope),
      signal: controller.signal,
    });
    return { transportOk: true, httpStatus: r.status };
  } catch (e) {
    return {
      transportOk: false,
      httpStatus: 0,
      reason: String(e?.message || e).slice(0, 100),
    };
  } finally {
    clearTimeout(timer);
  }
}

async function dryProbeRoute(env, route) {
  const common = {
    symbol: "BTCUSDT",
    quote_amount_usdt: 0,
    take_profit_price: 0,
    stop_loss_price: 0,
    stop_limit_price: 0,
    quantity: 0,
    order_list_id: 0,
    list_client_order_id: "",
    stop_client_order_id: "",
    limit_client_order_id: "",
    confirmed: false,
    dry_run: true,
  };
  const envelope = await signBridgeEnvelope(env, {
    ...common,
    signal_id: `dryprobe${String(route.id).toLowerCase()}${Date.now()}`,
    action: route.id === "BUY_V2" ? "BUY" : "OCO",
  });
  const first = await dispatchSignedEnvelope(route.url, envelope);
  const replay = await dispatchSignedEnvelope(route.url, envelope);
  return { routeId: route.id, first, replay };
}

async function snapshot(env) {
  const [ops, hb, rec, scheduler] = await Promise.all([
    computeState(env),
    getState(env, "ops:heartbeats"),
    getState(env, "ops:reconciliation:last"),
    getState(env, "ops:scheduler:last"),
  ]);
  return {
    ok: true,
    generatedAt: Date.now(),
    ops,
    heartbeats: hb || {},
    reconciliation: rec || null,
    scheduler: scheduler || null,
    liveTrading: false,
    autonomousExecution: false,
    noSecretValuesExposed: true,
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/supabase-relay-replay-selftest" && request.method === "POST") {
      const result=await supabaseRelayReplaySelftest(env);
      return Response.json(result,{
        status:result.ok?200:503,
        headers:{"cache-control":"no-store"},
      });
    }
    if (url.pathname === "/supabase-relay-preflight") {
      const result = await supabaseRelayReadOnlyPreflight(env);
      return Response.json({
        ...result,
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
      }, {
        status: result.ok ? 200 : 503,
        headers: { "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/vercel-immutable-relay-preflight") {
      const result = await vercelImmutableRelayReadOnlyPreflight(env);
      return Response.json({
        ...result,
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
      }, {
        status: result.ok ? 200 : 503,
        headers: { "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/cloudflare-binance-ws-preflight") {
      const result = await cloudflareBinanceWsAccountPreflight(env);
      return Response.json({
        ...result,
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
      }, {
        status: result.ok ? 200 : 503,
        headers: { "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/cloudflare-binance-pair-preflight") {
      const result = await cloudflareBinanceCredentialPairPreflight(env);
      return Response.json(result, {
        status: result.ok ? 200 : 503,
        headers: { "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/cloudflare-binance-direct-preflight") {
      const result = await cloudflareDirectBinanceReadOnlyPreflight(env);
      return Response.json({
        ...result,
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
      }, {
        status: result.ok ? 200 : 503,
        headers: { "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/free-transport-v2-preflight") {
      const result = await vercelTransportV2ReadOnlyPreflight(env);
      return Response.json({
        ...result,
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
      }, {
        status: result.ok ? 200 : 503,
        headers: { "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/infra-credential-presence") {
      const rawApiKey = String(env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "").trim();
      const apiKeyPresent = Boolean(rawApiKey);
      const apiSecretPresent = Boolean(env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY);
      let apiKeyFingerprint = null;
      if (rawApiKey) {
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(rawApiKey));
        apiKeyFingerprint = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
      }
      return Response.json({
        ok: true,
        cloudflareBinanceApiKeyPresent: apiKeyPresent,
        cloudflareBinanceApiKeyFingerprint: apiKeyFingerprint,
        cloudflareBinanceSecretPresent: apiSecretPresent,
        cloudflareBinanceCredentialsComplete: apiKeyPresent && apiSecretPresent,
        telegramRelaySecretPresent: Boolean(env.TELEGRAM_BOT_TOKEN),
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
        financialAction: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/make-bridge-verify" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const result = await verifyBridgeEnvelope(env, body);
      const routeId = bridgeRouteId(body);
      await recordBridgeRouteAudit(env, routeId, result.status);
      if (result.ok === true) {
        await putState(env, "bridge:last-verified", {
          at: Date.now(),
          route: "CLOUDFLARE_HMAC_MAKE",
          routeId,
          routeVersion: MAKE_EXECUTION_ROUTE.version,
        });
      }
      return Response.json(result, { status: result.ok ? 200 : 401, headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/bridge-auth-selftest" && request.method === "POST") {
      const input = await request.json().catch(() => ({}));
      const signed = await signBridgeEnvelope(env, {
        signal_id: String(input.signal_id || "selftest"),
        action: "DRY_AUTH_TEST",
        symbol: String(input.symbol || "BTCUSDT"),
        quote_amount_usdt: 0,
        take_profit_price: 0,
        stop_loss_price: 0,
        confirmed: false,
        dry_run: true,
      });
      const first = await verifyBridgeEnvelope(env, signed);
      const replay = await verifyBridgeEnvelope(env, signed);
      const passed = first.ok === true && replay.status === "REPLAY_BLOCKED";
      if (passed) {
        await putState(env, "bridge:health", { ok: true, at: Date.now(), route: "CLOUDFLARE_HMAC_MAKE" });
        await putState(env, "bridge:ownership", { owner: "MAKE_EXECUTOR_V2", at: Date.now(), exclusive: true });
      }
      return Response.json({
        ok: passed,
        first: first.status,
        replay: replay.status,
        oldVercelFallback: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/bridge-sign-dry-handshake" && request.method === "POST") {
      const signed = await signBridgeEnvelope(env, {
        signal_id: "prodhandshake",
        action: "DRY_AUTH_TEST",
        symbol: "BTCUSDT",
        quote_amount_usdt: 0,
        take_profit_price: 0,
        stop_loss_price: 0,
        stop_limit_price: 0,
        quantity: 0,
        order_list_id: 0,
        confirmed: false,
        dry_run: true,
      });
      return Response.json({
        ok: true,
        envelope: signed,
        financialAction: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/make-v2-dry-probe" && request.method === "POST") {
      const [buy, oco] = await Promise.all([
        dryProbeRoute(env, MAKE_EXECUTION_ROUTE.buy),
        dryProbeRoute(env, MAKE_EXECUTION_ROUTE.oco),
      ]);
      const now = Date.now();
      const [buyAudit, ocoAudit] = await Promise.all([
        getState(env, "bridge:route:BUY_V2"),
        getState(env, "bridge:route:OCO_V2"),
      ]);
      const buyVerified = buy?.first?.transportOk === true
        && buy?.replay?.transportOk === true
        && Number(buyAudit?.acceptedAt || 0) > 0
        && Number(buyAudit?.replayBlockedAt || 0) > 0
        && now - Number(buyAudit?.acceptedAt || 0) <= 60_000
        && now - Number(buyAudit?.replayBlockedAt || 0) <= 60_000;
      const ocoVerified = oco?.first?.transportOk === true
        && oco?.replay?.transportOk === true
        && Number(ocoAudit?.acceptedAt || 0) > 0
        && Number(ocoAudit?.replayBlockedAt || 0) > 0
        && now - Number(ocoAudit?.acceptedAt || 0) <= 60_000
        && now - Number(ocoAudit?.replayBlockedAt || 0) <= 60_000;
      const healthRefreshed = buyVerified && ocoVerified;
      if (healthRefreshed) {
        await recordReconciliation(env, {
          ok: true,
          open_orders_checked: 0,
          protected_orders_checked: 0,
          source: "MAKE_V2_DRY_PROBE",
        });
        await heartbeat(env, ["binance-readonly", "offsite-backup"], { source: "MAKE_V2_DRY_PROBE" });
        await computeState(env);
      }
      return Response.json({
        ok: true,
        status: "MAKE_V2_DRY_PROBE_DISPATCHED",
        routeVersion: MAKE_EXECUTION_ROUTE.version,
        buy,
        oco,
        buyVerified,
        ocoVerified,
        healthRefreshed,
        financialAction: false,
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/make-v2-route-status") {
      const [buy, oco] = await Promise.all([
        getState(env, "bridge:route:BUY_V2"),
        getState(env, "bridge:route:OCO_V2"),
      ]);
      return Response.json({
        ok: true,
        routeVersion: MAKE_EXECUTION_ROUTE.version,
        buyRouteId: MAKE_EXECUTION_ROUTE.buy.id,
        ocoRouteId: MAKE_EXECUTION_ROUTE.oco.id,
        buy: buy || null,
        oco: oco || null,
        legacyRoutesConfigured: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/bridge-auth-status") {
      return Response.json({
        ok: true,
        route: "CLOUDFLARE_HMAC_MAKE",
        hmac: "HMAC_SHA256",
        timestamp: true,
        nonceReplayProtection: true,
        secretVersion: "v2",
        oldVercelFallback: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/ops-heartbeat" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const requested = Array.isArray(body.components) ? body.components : [body.component || "binance-readonly"];
      const allowed = requested.map(String).filter((x) => ["binance-readonly", "reconciler", "protection", "offsite-backup"].includes(x));
      await heartbeat(env, allowed.length ? allowed : ["binance-readonly"], { source: String(body.source || "MAKE").slice(0, 80) });
      return Response.json({ ok: true, status: "HEARTBEAT_OK", liveTrading: false });
    }
    if (url.pathname === "/ops-reconciliation-report" && request.method === "POST") {
      const row = await recordReconciliation(env, await request.json().catch(() => ({})));
      return Response.json({ ok: true, status: "RECONCILIATION_RECORDED", reconciliation: row, liveTrading: false });
    }
    if (url.pathname === "/manual-live-e2e") {
      return Response.json({
        ok: false,
        status: "PUBLIC_MANUAL_LIVE_ENDPOINT_DISABLED",
        internalOneShotOnly: true,
        noSecretValuesExposed: true,
      }, { status: 404, headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/cutover-e2e-status") {
      const result = await getState(env, "cutover:e2e:result");
      return Response.json({
        ok: true,
        armed: String(env.E2E_ARMED || "").toLowerCase() === "true",
        liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
        manualOnly: true,
        automaticExecution: false,
        completed: result?.ok === true && result?.manual === true,
        result: result || null,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/go-no-go") {
      const ops = await computeState(env);
      const reconciliation = (await getState(env, "ops:reconciliation:last")) || null;
      const heartbeats = (await getState(env, "ops:heartbeats")) || {};
      const unknown = (await getState(env, "live:unknown-orders")) || [];
      const unprotected = (await getState(env, "live:unprotected-positions")) || [];
      const daily = (await getState(env, "risk:daily-live")) || { realizedLossUSDT: 0 };
      const bridgeHealth = (await getState(env, "bridge:health")) || null;
      const ownership = (await getState(env, "bridge:ownership")) || null;
      const provider = executionProvider(env);
      const configured = executorConfigured(env);
      const route = executionRoute(env);
      const now = Date.now();
      const hbAt = (name) => Number(typeof heartbeats?.[name] === "number" ? heartbeats[name] : heartbeats?.[name]?.at || 0);
      const snapshotFresh = hbAt("offsite-backup") > 0 && now - hbAt("offsite-backup") <= HEARTBEAT_STALE_MS;

      let routeHealthy = false;
      let ownershipFresh = false;
      let readonlyHeartbeat = null;
      let readonlyReconciliation = null;
      if (provider === "SUPABASE_V2") {
        [readonlyHeartbeat, readonlyReconciliation] = await Promise.all([
          executionReadOnlyHeartbeat(env),
          executionReadOnlyReconcile(env),
        ]);
        routeHealthy = readonlyHeartbeat?.transportOk === true
          && readonlyHeartbeat?.body?.canTrade === true
          && readonlyHeartbeat?.body?.financialAction === false
          && readonlyReconciliation?.ok === true
          && readonlyReconciliation?.financialAction === false;
        ownershipFresh = routeHealthy && configured;
        await recordReconciliation(env, {
          ok: readonlyReconciliation?.ok === true,
          reason: readonlyReconciliation?.ok === true ? null : String(readonlyReconciliation?.status || "SUPABASE_RECONCILIATION_FAILED"),
          open_orders_checked: Number(readonlyReconciliation?.openOrdersChecked || 0),
          protected_orders_checked: Number(readonlyReconciliation?.protectedOrderLists || 0),
          source: "SUPABASE_V2_GO_NO_GO_READONLY",
        });
        await putState(env, "live:unprotected-positions",
          Number(readonlyReconciliation?.orphanBotOrders || 0) > 0
            ? [{ source:"SUPABASE_V2", count:Number(readonlyReconciliation.orphanBotOrders || 0), at:Date.now() }]
            : []
        );
      } else {
        routeHealthy = bridgeHealth?.ok === true
          && now - Number(bridgeHealth?.at || 0) <= HEARTBEAT_STALE_MS;
        ownershipFresh = ownership?.owner === executionOwner(env)
          && ownership?.exclusive === true
          && now - Number(ownership?.at || 0) <= HEARTBEAT_STALE_MS;
      }

      const e2eResult = (await getState(env, "cutover:e2e:result")) || null;
      const e2eArmed = String(env.E2E_ARMED || "").toLowerCase() === "true";
      const manualE2EComplete = e2eResult?.ok === true && e2eResult?.manual === true;
      const gate = evaluateGoNoGo({
        supervisorState: ops.state,
        reconciliationOk: reconciliation?.ok === true && now - Number(reconciliation?.at || 0) <= HEARTBEAT_STALE_MS,
        snapshotFresh,
        watchdogHealthy: !ops.stale?.length,
        binanceConnectionOk: provider === "SUPABASE_V2"
          ? routeHealthy
          : hbAt("binance-readonly") > 0 && now - hbAt("binance-readonly") <= HEARTBEAT_STALE_MS,
        executionRouteHealthy: routeHealthy && configured,
        executorOwnershipOk: ownershipFresh,
        unknownOrders: Array.isArray(unknown) ? unknown.length : Number(unknown?.count || 0),
        unprotectedPositions: Array.isArray(unprotected) ? unprotected.length : Number(unprotected?.count || 0),
        dailyLossUSDT: -Math.abs(Number(daily.realizedLossUSDT || 0)),
        policy: readLivePolicy(env),
      });
      return Response.json({
        ok: true,
        ...gate,
        executionProvider: provider,
        executionRoute: route,
        routeVersion: routeVersion(env),
        bridgeFresh: routeHealthy,
        executorOwnershipOk: ownershipFresh,
        offsiteSnapshotFresh: snapshotFresh,
        executorConfigured: configured,
        e2eArmed,
        manualE2EComplete,
        readonlyHeartbeat: provider === "SUPABASE_V2" ? {
          transportOk: readonlyHeartbeat?.transportOk === true,
          canTrade: readonlyHeartbeat?.body?.canTrade === true,
          financialAction: false,
        } : null,
        readonlyReconciliation: provider === "SUPABASE_V2" ? {
          ok: readonlyReconciliation?.ok === true,
          openOrdersChecked: Number(readonlyReconciliation?.openOrdersChecked || 0),
          protectedOrderLists: Number(readonlyReconciliation?.protectedOrderLists || 0),
          orphanBotOrders: Number(readonlyReconciliation?.orphanBotOrders || 0),
          financialAction: false,
        } : null,
        manualExecutionAllowed: gate.go
          && readLivePolicy(env).liveExecutionEnabled === true
          && readLivePolicy(env).autonomousEnabled !== true
          && (e2eArmed || manualE2EComplete),
        liveTrading: readLivePolicy(env).liveExecutionEnabled === true,
        autonomousExecution: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/ops-status") {
      const ops = await computeState(env);
      return Response.json({
        ok: true,
        ...ops,
        persistentScheduler: true,
        deadManMonitoring: true,
        externalHeartbeatCadenceSeconds: 900,
        recoveryStateMachine: true,
        alertEscalation: true,
        liveTrading: false,
        autonomousExecution: false,
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/ops-snapshot") {
      return Response.json(await snapshot(env), { headers: { "cache-control": "no-store" } });
    }
    return worker.fetch(request, env, ctx);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      if (typeof worker.scheduled === "function") await worker.scheduled(event, env, ctx);
      await heartbeat(env, ["scanner", "market-data", "strategy", "risk", "watchdog"], { source: "CLOUDFLARE_CRON" });
      await putState(env, "ops:scheduler:last", { at: Date.now(), source: "CLOUDFLARE_CRON" });
      const next = await computeState(env);
      await alertTransition(env, next);

      const executorConfiguredNow = executorConfigured(env);
      const providerNow = executionProvider(env);
      const watchdogKey = providerNow === "SUPABASE_V2"
        ? "supabase:combined-watchdog:last-dispatch"
        : "make:combined-watchdog:last-dispatch";
      const lastWatchdog = (await getState(env, watchdogKey)) || null;
      if (executorConfiguredNow && Date.now() - Number(lastWatchdog?.at || 0) >= 14 * 60 * 1000) {
        const watchdog = await executionReadOnlyHeartbeat(env);
        const now = Date.now();
        let operationalOk = false;
        let buyVerified = false;
        let ocoVerified = false;
        let reconciliation = null;

        if (providerNow === "SUPABASE_V2") {
          reconciliation = await executionReadOnlyReconcile(env);
          operationalOk = watchdog?.transportOk === true
            && watchdog?.body?.canTrade === true
            && watchdog?.body?.financialAction === false
            && reconciliation?.ok === true;
          buyVerified = operationalOk;
          ocoVerified = operationalOk;

          await recordReconciliation(env, {
            ok: reconciliation?.ok === true,
            reason: reconciliation?.ok === true ? null : String(reconciliation?.status || "SUPABASE_RECONCILIATION_FAILED"),
            open_orders_checked: Number(reconciliation?.openOrdersChecked || 0),
            protected_orders_checked: Number(reconciliation?.protectedOrderLists || 0),
            source: "SUPABASE_V2_READONLY_RECONCILIATION",
          });
          await putState(env, "live:unprotected-positions",
            Number(reconciliation?.orphanBotOrders || 0) > 0
              ? [{ source:"SUPABASE_V2", count:Number(reconciliation.orphanBotOrders || 0), at:now }]
              : []
          );
        } else {
          const [buyAudit, ocoAudit] = await Promise.all([
            getState(env, "bridge:route:BUY_V2"),
            getState(env, "bridge:route:OCO_V2"),
          ]);
          buyVerified = watchdog?.buy?.transportOk === true
            && String(buyAudit?.lastStatus || "") === "BRIDGE_AUTH_OK"
            && now - Number(buyAudit?.acceptedAt || 0) <= 60_000;
          ocoVerified = watchdog?.oco?.transportOk === true
            && String(ocoAudit?.lastStatus || "") === "BRIDGE_AUTH_OK"
            && now - Number(ocoAudit?.acceptedAt || 0) <= 60_000;
          operationalOk = buyVerified && ocoVerified;

          if (!operationalOk) {
            await recordReconciliation(env, {
              ok: false,
              reason: "MAKE_V2_WATCHDOG_VERIFICATION_FAILED",
              open_orders_checked: 0,
              protected_orders_checked: 0,
              source: "MAKE_V2_READONLY_WATCHDOG",
            });
          }
        }

        if (operationalOk) {
          await putState(env, "bridge:health", {
            ok: true,
            at: now,
            route: executionRoute(env),
            routeVersion: routeVersion(env),
            source: providerNow === "SUPABASE_V2" ? "SUPABASE_V2_READONLY_WATCHDOG" : "MAKE_V2_READONLY_WATCHDOG",
          });
          await putState(env, "bridge:ownership", {
            owner: executionOwner(env),
            at: now,
            exclusive: true,
            routeVersion: routeVersion(env),
            source: providerNow === "SUPABASE_V2" ? "SUPABASE_V2_READONLY_WATCHDOG" : "MAKE_V2_READONLY_WATCHDOG",
          });
          await heartbeat(env, ["binance-readonly", "offsite-backup"], {
            source: providerNow === "SUPABASE_V2" ? "SUPABASE_V2_READONLY_WATCHDOG" : "MAKE_V2_READONLY_WATCHDOG"
          });
        }

        const ids = executionRouteIds(env);
        await putState(env, watchdogKey, {
          at: now,
          provider: providerNow,
          transportOk: watchdog?.transportOk === true,
          operationalOk,
          buyRouteId: ids.buy,
          ocoRouteId: ids.oco,
          routeVersion: routeVersion(env),
          buyVerified,
          ocoVerified,
          reconciliationOk: providerNow === "SUPABASE_V2" ? reconciliation?.ok === true : null,
          openOrdersChecked: providerNow === "SUPABASE_V2" ? Number(reconciliation?.openOrdersChecked || 0) : null,
          protectedOrderLists: providerNow === "SUPABASE_V2" ? Number(reconciliation?.protectedOrderLists || 0) : null,
          orphanBotOrders: providerNow === "SUPABASE_V2" ? Number(reconciliation?.orphanBotOrders || 0) : null,
          httpStatus: Number(watchdog?.httpStatus || 0),
          status: String(watchdog?.body?.status || watchdog?.status || "UNKNOWN").slice(0, 80),
        });
      }

      const e2eArmed = String(env.E2E_ARMED || "").toLowerCase() === "true";
      if (e2eArmed) {
        await putState(env, "cutover:e2e:armed-status", {
          at: Date.now(),
          manualOnly: true,
          automaticExecution: false,
          liveExecutionEnabled: readLivePolicy(env).liveExecutionEnabled === true,
          autonomousEnabled: readLivePolicy(env).autonomousEnabled === true,
        });
      }
    })());
  },
};
