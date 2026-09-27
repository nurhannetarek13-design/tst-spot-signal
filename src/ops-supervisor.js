import worker, { SignalState } from "./buy-gateway-auth-wrapper.js";
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
function hbAt(hb, name) {
  const row = hb?.[name];
  return Number(typeof row === "number" ? row : row?.at || 0);
}
async function computeState(env) {
  const now = Date.now();
  const hb = (await getState(env, "ops:heartbeats")) || {};
  const previous = (await getState(env, "ops:state")) || { state: "WARMING_UP", since: now };
  const reconciliation = (await getState(env, "ops:reconciliation:last")) || null;
  const scheduler = (await getState(env, "ops:scheduler:last")) || null;
  const critical = ["scanner", "market-data", "strategy", "risk", "watchdog", "binance-readonly", "reconciler", "protection"];
  const stale = critical.filter((c) => !hbAt(hb, c) || now - hbAt(hb, c) > HEARTBEAT_STALE_MS);

  let state = "HEALTHY";
  let reason = null;
  if (stale.length) {
    state = "DEGRADED";
    reason = `STALE_HEARTBEAT:${stale.join(",")}`;
  } else if (reconciliation?.ok === false) {
    state = "PROTECTION_ONLY";
    reason = reconciliation.reason || "RECONCILIATION_FAILED";
  } else if (["DEGRADED", "PROTECTION_ONLY"].includes(previous.state)) {
    state = "RECOVERING";
    reason = "RECOVERY_STABILIZATION";
  } else if (previous.state === "RECOVERING") {
    if (now - Number(previous.since || now) < RECOVERY_HOLD_MS) {
      state = "RECOVERING";
      reason = "RECOVERY_STABILIZATION";
    } else {
      state = "RECONCILING";
      reason = "POST_RECOVERY_RECONCILIATION";
    }
  } else if (previous.state === "RECONCILING") {
    if (!reconciliation || now - Number(reconciliation.at || 0) > HEARTBEAT_STALE_MS) {
      state = "RECONCILING";
      reason = "WAITING_FOR_FRESH_RECONCILIATION";
    } else if (reconciliation.ok !== true) {
      state = "PROTECTION_ONLY";
      reason = reconciliation.reason || "RECONCILIATION_FAILED";
    } else {
      state = "WARMING_UP";
      reason = "POST_RECOVERY_WARMUP";
    }
  } else if (previous.state === "WARMING_UP" && now - Number(previous.since || now) < WARMUP_MS) {
    state = "WARMING_UP";
    reason = "WARMUP_WINDOW";
  }

  const next = {
    state,
    reason,
    stale,
    since: previous.state === state ? previous.since : now,
    checkedAt: now,
    schedulerLastAt: Number(scheduler?.at || 0) || null,
    reconciliationAt: Number(reconciliation?.at || 0) || null,
    newEntriesAllowed: false,
    liveTrading: false,
    autonomousExecution: false,
  };
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
