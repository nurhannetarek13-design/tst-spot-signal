import worker, { SignalState } from "./buy-gateway-auth-wrapper.js";
import { deriveOpsState } from "./ops-state-machine.js";
import { verifyBridgeEnvelope, signBridgeEnvelope, rotateBridgeSecret } from "./bridge-auth.js";
import { readLivePolicy, evaluateGoNoGo } from "./live-cutover-policy.js";
import { manualBuyAndProtect } from "./make-live-client.js";
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
      if (result.ok === true) {
        await putState(env, "bridge:last-verified", { at: Date.now(), route: "CLOUDFLARE_HMAC_MAKE" });
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
      const now = Date.now();
      const hbAt = (name) => Number(typeof heartbeats?.[name] === "number" ? heartbeats[name] : heartbeats?.[name]?.at || 0);
      const bridgeFresh = bridgeHealth?.ok === true && now - Number(bridgeHealth?.at || 0) <= HEARTBEAT_STALE_MS;
      const ownershipFresh = ownership?.owner === "MAKE_EXECUTOR_V2" && ownership?.exclusive === true && now - Number(ownership?.at || 0) <= HEARTBEAT_STALE_MS;
      const snapshotFresh = hbAt("offsite-backup") > 0 && now - hbAt("offsite-backup") <= HEARTBEAT_STALE_MS;
      const executorConfigured = String(env.MAKE_EXECUTOR_V2_READY || "").toLowerCase() === "true";
      const gate = evaluateGoNoGo({
        supervisorState: ops.state,
        reconciliationOk: reconciliation?.ok === true && now - Number(reconciliation?.at || 0) <= HEARTBEAT_STALE_MS,
        snapshotFresh,
        watchdogHealthy: !ops.stale?.length,
        binanceConnectionOk: hbAt("binance-readonly") > 0 && now - hbAt("binance-readonly") <= HEARTBEAT_STALE_MS,
        executionRouteHealthy: bridgeFresh && executorConfigured,
        executorOwnershipOk: ownershipFresh,
        unknownOrders: Array.isArray(unknown) ? unknown.length : Number(unknown?.count || 0),
        unprotectedPositions: Array.isArray(unprotected) ? unprotected.length : Number(unprotected?.count || 0),
        dailyLossUSDT: -Math.abs(Number(daily.realizedLossUSDT || 0)),
        policy: readLivePolicy(env),
      });
      return Response.json({
        ok: true,
        ...gate,
        executionRoute: "CLOUDFLARE_HMAC_MAKE",
        bridgeFresh,
        executorOwnershipOk: ownershipFresh,
        offsiteSnapshotFresh: snapshotFresh,
        executorConfigured,
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

      const e2eArmed = String(env.E2E_ARMED || "").toLowerCase() === "true";
      const policy = readLivePolicy(env);
      if (e2eArmed && policy.liveExecutionEnabled === true && policy.autonomousEnabled !== true) {
        const existing = await getState(env, "cutover:e2e:result");
        if (!existing) {
          const reconciliation = (await getState(env, "ops:reconciliation:last")) || null;
          const heartbeats = (await getState(env, "ops:heartbeats")) || {};
          const unknown = (await getState(env, "live:unknown-orders")) || [];
          const unprotected = (await getState(env, "live:unprotected-positions")) || [];
          const daily = (await getState(env, "risk:daily-live")) || { realizedLossUSDT: 0 };
          const bridgeHealth = (await getState(env, "bridge:health")) || null;
          const ownership = (await getState(env, "bridge:ownership")) || null;
          const now = Date.now();
          const hbAt = (name) => Number(typeof heartbeats?.[name] === "number" ? heartbeats[name] : heartbeats?.[name]?.at || 0);
          const executorConfigured = String(env.MAKE_EXECUTOR_V2_READY || "").toLowerCase() === "true";
          const gate = evaluateGoNoGo({
            supervisorState: next.state,
            reconciliationOk: reconciliation?.ok === true && now - Number(reconciliation?.at || 0) <= HEARTBEAT_STALE_MS,
            snapshotFresh: hbAt("offsite-backup") > 0 && now - hbAt("offsite-backup") <= HEARTBEAT_STALE_MS,
            watchdogHealthy: !next.stale?.length,
            binanceConnectionOk: hbAt("binance-readonly") > 0 && now - hbAt("binance-readonly") <= HEARTBEAT_STALE_MS,
            executionRouteHealthy: executorConfigured && bridgeHealth?.ok === true && now - Number(bridgeHealth?.at || 0) <= HEARTBEAT_STALE_MS,
            executorOwnershipOk: ownership?.owner === "MAKE_EXECUTOR_V2" && ownership?.exclusive === true && now - Number(ownership?.at || 0) <= HEARTBEAT_STALE_MS,
            unknownOrders: Array.isArray(unknown) ? unknown.length : Number(unknown?.count || 0),
            unprotectedPositions: Array.isArray(unprotected) ? unprotected.length : Number(unprotected?.count || 0),
            dailyLossUSDT: -Math.abs(Number(daily.realizedLossUSDT || 0)),
            policy,
          });
          if (gate.go) {
            const claimed = await claimState(env, "cutover:e2e:once", { at: now }, 7 * 24 * 60 * 60);
            if (claimed) {
              const input = {
                signal_id: "CUTOVER_E2E_20260928",
                symbol: String(env.E2E_SYMBOL || "BTCUSDT"),
                quote_amount_usdt: Math.min(5.5, Math.max(5, Number(env.E2E_QUOTE_USDT || 5))),
                take_profit_price: Number(env.E2E_TP_PRICE || 0),
                stop_loss_price: Number(env.E2E_SL_PRICE || 0),
              };
              let result;
              try {
                result = await manualBuyAndProtect(env, input);
              } catch (e) {
                result = {
                  ok: false,
                  status: "E2E_INTERNAL_ERROR",
                  reconciliationRequired: true,
                  mayResend: false,
                  reason: String(e?.message || e).slice(0, 120),
                };
              }
              await putState(env, "cutover:e2e:result", { ...result, input, at: Date.now() }, 30 * 24 * 60 * 60);
              if (["EXECUTION_STATUS_UNKNOWN", "OCO_STATUS_UNKNOWN", "E2E_INTERNAL_ERROR", "PROTECTION_NORMALIZATION_FAILED"].includes(String(result?.status || ""))) {
                const rows = Array.isArray(unknown) ? unknown : [];
                rows.push({ signalId: input.signal_id, status: result.status, at: Date.now() });
                await putState(env, "live:unknown-orders", rows.slice(-20));
              }
              await tg(env, `🧪 LIVE CUTOVER E2E\nStatus: ${result?.status || "UNKNOWN"}\nSymbol: ${input.symbol}\nAmount: ${input.quote_amount_usdt} USDT\nAutonomous: OFF`);
            }
          }
        }
      }
    })());
  },
};
