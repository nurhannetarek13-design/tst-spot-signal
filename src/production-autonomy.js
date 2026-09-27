import worker, { SignalState } from "./buy-gateway-auth-wrapper.js";
export { SignalState };

const MAX_LIVE_BUY_USDT = 5.50;
const MAX_LIVE_RISK_USDT = 0.20;
const MAX_SIGNAL_AGE_MS = 5 * 60 * 1000;
const HEARTBEAT_STALE_MS = 3 * 60 * 1000;
const WARMUP_MS = 2 * 60 * 1000;
const INTENT_TTL_SEC = 10 * 60;
const STATE_TTL_SEC = 30 * 24 * 60 * 60;
const MIN_SCORE = 90;

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
async function claimState(env, key, value, ttl) {
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
function autonomousEnabled(env) {
  return String(env.LIVE_AUTONOMOUS ?? "true").toLowerCase() === "true";
}
function livePositions(rows) {
  return (Array.isArray(rows) ? rows : []).filter((x) => x && x.closed !== true);
}
async function heartbeat(env, components) {
  const now = Date.now();
  const current = (await getState(env, "ops:heartbeats")) || {};
  for (const c of components) current[String(c)] = now;
  await putState(env, "ops:heartbeats", current);
  return current;
}
async function baseJson(env, path) {
  const r = await worker.fetch(new Request(`https://internal${path}`), env, { waitUntil() {} });
  return r.json().catch(() => ({}));
}
async function computeOpsState(env) {
  const now = Date.now();
  const hb = (await getState(env, "ops:heartbeats")) || {};
  const positions = livePositions(await getState(env, "live:positions"));
  const halt = await getState(env, "ops:halt");
  const previous = (await getState(env, "ops:state")) || { state: "WARMING_UP", since: now };
  const critical = ["scanner", "risk", "watchdog", "executor", "reconciler", "protection"];
  const stale = critical.filter((c) => !Number(hb[c]) || now - Number(hb[c]) > HEARTBEAT_STALE_MS);
  const unprotected = positions.some((p) => p.protectionStatus !== "PROTECTED");
  let state = "HEALTHY";
  let reason = null;

  if (halt?.active) {
    state = "PROTECTION_ONLY";
    reason = halt.reason || "MANUAL_OR_SAFETY_HALT";
  } else if (unprotected) {
    state = "PROTECTION_ONLY";
    reason = "UNPROTECTED_POSITION";
  } else if (stale.length) {
    state = "DEGRADED";
    reason = `STALE_HEARTBEAT:${stale.join(",")}`;
  } else if (["DEGRADED", "PROTECTION_ONLY", "RECOVERING", "RECONCILING"].includes(previous.state)) {
    state = "RECOVERING";
    reason = "RECOVERY_STABILIZATION";
  } else if (previous.state === "RECOVERING") {
    state = "RECONCILING";
    reason = "POST_RECOVERY_RECONCILIATION";
  } else if (previous.state === "RECONCILING") {
    state = "WARMING_UP";
    reason = "POST_RECOVERY_WARMUP";
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
    autonomousEnabled: autonomousEnabled(env),
    newEntriesAllowed: autonomousEnabled(env) && state === "HEALTHY",
    openLivePositions: positions.length,
  };
  await putState(env, "ops:state", next);
  return next;
}
async function alertTransition(env, ops) {
  const last = await getState(env, "ops:last-alert-state");
  const critical = ["DEGRADED", "PROTECTION_ONLY"];
  if (!last || last.state !== ops.state || last.reason !== ops.reason) {
    await putState(env, "ops:last-alert-state", { state: ops.state, reason: ops.reason, at: Date.now() });
    if (critical.includes(ops.state)) {
      await tg(env, `🚨 24/7 OPS — ${ops.state}\nReason: ${ops.reason || "UNKNOWN"}\nNO NEW ENTRIES until recovery completes.`);
    } else if (ops.state === "HEALTHY" && last && critical.includes(last.state)) {
      await tg(env, "✅ 24/7 OPS RECOVERED\nReconciliation/warmup completed. New entries may resume only if all trading gates pass.");
    }
  }
}
async function makeIntent(env) {
  await heartbeat(env, ["executor"]);
  const ops = await computeOpsState(env);
  if (!ops.newEntriesAllowed) return { ok: true, action: "NO_TRADE", reason: ops.state, ops };

  const positions = livePositions(await getState(env, "live:positions"));
  if (positions.length) return { ok: true, action: "NO_TRADE", reason: "LIVE_POSITION_OPEN", ops };

  const fusion = await baseJson(env, "/fusion-status");
  if (fusion.liveReady !== true) {
    return { ok: true, action: "NO_TRADE", reason: "STRATEGY_VALIDATION_NOT_LIVE_READY", ops };
  }

  const paper = await baseJson(env, "/paper-status");
  if (Number(paper?.dailyRealizedPnlUSDT ?? 0) <= -0.50) {
    return { ok: true, action: "NO_TRADE", reason: "DAILY_LOSS_LIMIT", ops };
  }

  const active = await getState(env, "paper:active");
  const p = Array.isArray(active) ? active[0] : null;
  if (!p) return { ok: true, action: "NO_TRADE", reason: "NO_SIGNAL", ops };

  const age = Date.now() - Number(p.openedAt || 0);
  if (!(age >= 0 && age <= MAX_SIGNAL_AGE_MS)) return { ok: true, action: "NO_TRADE", reason: "STALE_SIGNAL", ops };
  if (Number(p.score || 0) < MIN_SCORE) return { ok: true, action: "NO_TRADE", reason: "SCORE_BELOW_LIVE_FLOOR", ops };
  if (!(Number(p.stop) < Number(p.entry) && Number(p.target) > Number(p.entry))) {
    return { ok: true, action: "NO_TRADE", reason: "INVALID_LEVELS", ops };
  }
  if (Number(p.riskUSDT || 0) > MAX_LIVE_RISK_USDT + 1e-9) {
    return { ok: true, action: "NO_TRADE", reason: "RISK_CAP", ops };
  }

  const quote = Math.min(MAX_LIVE_BUY_USDT, Number(p.notional || MAX_LIVE_BUY_USDT));
  if (!(quote >= 5 && quote <= MAX_LIVE_BUY_USDT)) return { ok: true, action: "NO_TRADE", reason: "NOTIONAL_GATE", ops };

  const raw = `${p.symbol}:${p.strategy}:${p.signalBar || p.openedAt}`;
  const intentId = raw.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 48);
  const claim = await claimState(env, `live:intent-claim:${intentId}`, { at: Date.now() }, INTENT_TTL_SEC);
  if (!claim) return { ok: true, action: "NO_TRADE", reason: "INTENT_ALREADY_CLAIMED", ops };

  const nonce = crypto.randomUUID();
  const intent = {
    ok: true,
    action: "BUY",
    intent_id: intentId,
    nonce,
    symbol: String(p.symbol),
    quote_amount_usdt: Number(quote.toFixed(2)),
    reference_entry: Number(p.entry),
    stop_loss_price: Number(p.stop),
    take_profit_price: Number(p.target),
    strategy: String(p.strategy || "UNKNOWN"),
    score: Number(p.score || 0),
    risk_usdt: Number(p.riskUSDT || 0),
    client_order_id: `TSTA${intentId}`.slice(0, 32),
    created_at: Date.now(),
    expires_at: Date.now() + 2 * 60 * 1000,
  };
  await putState(env, `live:intent:${intentId}`, intent, INTENT_TTL_SEC);
  return intent;
}
async function applyAck(env, body) {
  const id = String(body?.intent_id || "");
  const intent = await getState(env, `live:intent:${id}`);
  if (!intent || String(body?.nonce || "") !== String(intent.nonce || "")) {
    return { ok: false, status: "ACK_REJECTED" };
  }
  const once = await claimState(env, `live:ack:${id}:${String(body?.status || "")}`, { at: Date.now() }, 86400);
  if (!once) return { ok: true, status: "ACK_DUPLICATE_IGNORED" };

  await heartbeat(env, ["executor", "reconciler", "protection"]);
  const status = String(body?.status || "");
  const rows = Array.isArray(await getState(env, "live:positions")) ? await getState(env, "live:positions") : [];
  if (status === "BUY_PROTECTED") {
    rows.push({
      intentId: id,
      symbol: intent.symbol,
      strategy: intent.strategy,
      entryRef: intent.reference_entry,
      expectedQty: Number(body.executed_qty || 0),
      quoteSpent: Number(body.quote_spent || 0),
      buyOrderId: String(body.buy_order_id || ""),
      ocoOrderListId: String(body.oco_order_list_id || ""),
      protectedQty: Number(body.protected_qty || body.executed_qty || 0),
      protectionStatus: "PROTECTED",
      openedAt: Date.now(),
      closed: false,
    });
    await putState(env, "live:positions", rows.slice(-20));
    await putState(env, "ops:halt", null, 1);
  } else if (status === "PROTECTION_FAILED_EMERGENCY_CLOSED") {
    await tg(env, `⚠️ ${intent.symbol}: BUY occurred but OCO failed; emergency market close completed. No new entries until recovery cycle finishes.`);
    await putState(env, "ops:state", { state: "RECOVERING", reason: status, since: Date.now(), checkedAt: Date.now() });
  } else if (status === "PROTECTION_FAILED_EMERGENCY_CLOSE_FAILED") {
    rows.push({
      intentId: id, symbol: intent.symbol, strategy: intent.strategy,
      expectedQty: Number(body.executed_qty || 0), protectedQty: 0,
      protectionStatus: "MISSING", openedAt: Date.now(), closed: false,
    });
    await putState(env, "live:positions", rows.slice(-20));
    await putState(env, "ops:halt", { active: true, reason: status, at: Date.now() });
    await tg(env, `🚨 CRITICAL — ${intent.symbol}\nOCO failed AND emergency close failed. New entries are blocked. Check Binance immediately.`);
  } else if (status === "BUY_FAILED") {
    await putState(env, "ops:state", { state: "RECOVERING", reason: "BUY_FAILED", since: Date.now(), checkedAt: Date.now() });
  }
  await putState(env, `live:execution-result:${id}`, { ...body, recordedAt: Date.now() }, 30 * 24 * 60 * 60);
  return { ok: true, status: "ACK_RECORDED" };
}
async function protectionPlan(env) {
  await heartbeat(env, ["reconciler", "protection"]);
  const positions = livePositions(await getState(env, "live:positions"));
  if (!positions.length) return { ok: true, active: false };
  const p = positions[0];
  return {
    ok: true,
    active: true,
    intent_id: p.intentId,
    symbol: p.symbol,
    expected_qty: p.expectedQty,
    protected_qty: p.protectedQty,
    oco_order_list_id: p.ocoOrderListId,
  };
}
async function protectionReport(env, body) {
  await heartbeat(env, ["reconciler", "protection"]);
  const positions = Array.isArray(await getState(env, "live:positions")) ? await getState(env, "live:positions") : [];
  const p = positions.find((x) => x && x.closed !== true && x.intentId === String(body?.intent_id || ""));
  if (!p) return { ok: false, status: "POSITION_NOT_FOUND" };
  const idMatch = String(body?.oco_order_list_id || "") === String(p.ocoOrderListId || "");
  const active = body?.oco_active === true || String(body?.oco_active).toLowerCase() === "true";
  const qtyMatch = Math.abs(Number(p.expectedQty || 0) - Number(p.protectedQty || 0)) <= Math.max(1e-12, Number(p.expectedQty || 0) * 0.0015);
  if (idMatch && active && qtyMatch) {
    p.protectionStatus = "PROTECTED";
    p.lastProtectionCheckAt = Date.now();
    await putState(env, "live:positions", positions);
    return { ok: true, status: "PROTECTION_OK" };
  }
  p.protectionStatus = "MISSING";
  p.lastProtectionCheckAt = Date.now();
  await putState(env, "live:positions", positions);
  await putState(env, "ops:halt", { active: true, reason: "PROTECTION_MISMATCH", at: Date.now() });
  await tg(env, `🚨 PROTECTION_FAILED — ${p.symbol}\nExpected OCO protection is missing/mismatched. New entries blocked.`);
  return { ok: false, status: "PROTECTION_MISMATCH" };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/autonomous-intent") {
      return Response.json(await makeIntent(env), { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/autonomous-ack" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const out = await applyAck(env, body);
      return Response.json(out, { status: out.ok ? 200 : 409, headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/ops-heartbeat" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const comps = Array.isArray(body.components) ? body.components : [body.component || "executor"];
      const allowed = comps.map(String).filter((x) => ["executor", "reconciler", "protection"].includes(x));
      await heartbeat(env, allowed.length ? allowed : ["executor"]);
      return Response.json({ ok: true, status: "HEARTBEAT_OK" });
    }
    if (url.pathname === "/ops-protection-plan") {
      return Response.json(await protectionPlan(env), { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname === "/ops-protection-report" && request.method === "POST") {
      return Response.json(await protectionReport(env, await request.json().catch(() => ({}))));
    }
    if (url.pathname === "/ops-status") {
      const ops = await computeOpsState(env);
      return Response.json({
        ok: true,
        ...ops,
        maxLiveBuyUSDT: MAX_LIVE_BUY_USDT,
        maxLiveRiskUSDT: MAX_LIVE_RISK_USDT,
        liveAutonomous: autonomousEnabled(env),
        splitBrainPolicy: "SINGLE_MAKE_EXECUTOR_PULL_LEASE",
        noSecretValuesExposed: true,
      }, { headers: { "cache-control": "no-store" } });
    }
    return worker.fetch(request, env, ctx);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      if (typeof worker.scheduled === "function") await worker.scheduled(event, env, ctx);
      await heartbeat(env, ["scanner", "strategy", "risk", "watchdog"]);
      const ops = await computeOpsState(env);
      await alertTransition(env, ops);
      await putState(env, "ops:scheduler:last", { at: Date.now(), state: ops.state });
    })());
  },
};