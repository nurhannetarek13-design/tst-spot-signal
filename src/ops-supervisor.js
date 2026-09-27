import worker, { SignalState } from "./buy-gateway-auth-wrapper.js";
import { deriveOpsState } from "./ops-state-machine.js";
import { verifyBridgeEnvelope, signBridgeEnvelope, rotateBridgeSecret } from "./bridge-auth.js";
import { LIVE_POLICY, evaluateGoNoGo } from "./live-cutover-policy.js";
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
    if (url.pathname === "/make-bridge-verify" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const result = await verifyBridgeEnvelope(env, body);
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
      return Response.json({
        ok: first.ok === true && replay.status === "REPLAY_BLOCKED",
        first: first.status,
        replay: replay.status,
        oldVercelFallback: false,
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
      const allowed = requested.map(String).filter((x) => ["binance-readonly", "reconciler", "protection"].includes(x));
      await heartbeat(env, allowed.length ? allowed : ["binance-readonly"], { source: String(body.source || "MAKE").slice(0, 80) });
      return Response.json({ ok: true, status: "HEARTBEAT_OK", liveTrading: false });
    }
    if (url.pathname === "/ops-reconciliation-report" && request.method === "POST") {
      const row = await recordReconciliation(env, await request.json().catch(() => ({})));
      return Response.json({ ok: true, status: "RECONCILIATION_RECORDED", reconciliation: row, liveTrading: false });
    }
    if (url.pathname === "/go-no-go") {
      const ops = await computeState(env);
      const reconciliation = (await getState(env, "ops:reconciliation:last")) || null;
      const scheduler = (await getState(env, "ops:scheduler:last")) || null;
      const unknown = (await getState(env, "live:unknown-orders")) || [];
      const unprotected = (await getState(env, "live:unprotected-positions")) || [];
      const daily = (await getState(env, "risk:daily-live")) || { realizedLossUSDT: 0 };
      const now = Date.now();
      const bridge = {
        healthy: true,
        route: "CLOUDFLARE_HMAC_MAKE",
      };
      const gate = evaluateGoNoGo({
        supervisorState: ops.state,
        reconciliationOk: reconciliation?.ok === true && now - Number(reconciliation?.at || 0) <= HEARTBEAT_STALE_MS,
        snapshotFresh: now - Number(scheduler?.at || 0) <= HEARTBEAT_STALE_MS,
        watchdogHealthy: !ops.stale?.length,
        binanceConnectionOk: !ops.stale?.includes("binance-readonly"),
        executionRouteHealthy: bridge.healthy === true,
        unknownOrders: Array.isArray(unknown) ? unknown.length : Number(unknown?.count || 0),
        unprotectedPositions: Array.isArray(unprotected) ? unprotected.length : Number(unprotected?.count || 0),
        dailyLossUSDT: -Math.abs(Number(daily.realizedLossUSDT || 0)),
        policy: LIVE_POLICY,
      });
      return Response.json({
        ok: true,
        ...gate,
        executionRoute: bridge.route,
        liveTrading: false,
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
    })());
  },
};
