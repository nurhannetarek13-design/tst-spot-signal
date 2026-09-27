import { signBridgeEnvelope } from "./bridge-auth.js";
import { readLivePolicy } from "./live-cutover-policy.js";
import { normalizeSpotProtection } from "./binance-spot-filters.js";

const BUY_URL = "https://hook.eu1.make.com/r9cqpv68bbj2zb01jitfmzk8iqb2zh16";
const OCO_URL = "https://hook.eu1.make.com/yn7vndobsf378u5hbec7jfnuam25ulcj";

function clientIds(signalId) {
  const base = String(signalId || "sig").replace(/[^A-Za-z0-9]/g, "").slice(0, 20) || "sig";
  return {
    list_client_order_id: `TSTL${base}`.slice(0, 32),
    stop_client_order_id: `TSTS${base}`.slice(0, 32),
    limit_client_order_id: `TSTT${base}`.slice(0, 32),
  };
}

async function postSigned(env, url, payload, timeoutMs = 20000) {
  const signed = await signBridgeEnvelope(env, payload);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(signed),
      signal: controller.signal,
    });
    const text = await r.text();
    let body = {};
    try { body = JSON.parse(text || "{}"); } catch { body = { status: "BAD_RESPONSE" }; }
    return { transportOk: r.ok, httpStatus: r.status, body };
  } catch (e) {
    return { transportOk: false, unknown: true, status: "EXECUTION_STATUS_UNKNOWN", reason: String(e?.message || e) };
  } finally {
    clearTimeout(timer);
  }
}

export async function manualBuyAndProtect(env, input) {
  const policy = readLivePolicy(env);
  if (policy.liveExecutionEnabled !== true) {
    return { ok: false, status: "LIVE_EXECUTION_DISABLED", noOrderSent: true };
  }
  const quote = Math.min(Number(input.quote_amount_usdt || 0), Number(policy.maxOrderUSDT));
  if (!(quote >= 5 && quote <= 5.5)) return { ok: false, status: "ORDER_SIZE_BLOCKED", noOrderSent: true };

  const common = {
    signal_id: String(input.signal_id || ""),
    symbol: String(input.symbol || "").toUpperCase(),
    take_profit_price: Number(input.take_profit_price || 0),
    stop_loss_price: Number(input.stop_loss_price || 0),
    confirmed: true,
    dry_run: false,
  };
  const buy = await postSigned(env, BUY_URL, { ...common, action: "BUY", quote_amount_usdt: quote });
  if (buy.unknown) return { ok: false, status: "EXECUTION_STATUS_UNKNOWN", reconciliationRequired: true, mayResend: false };
  if (!buy.transportOk || buy.body?.status !== "BUY_FILLED") return { ok: false, status: buy.body?.status || "BUY_REJECTED", buy };

  const qty = Number(buy.body.executed_qty || 0);
  if (!(qty > 0)) return { ok: false, status: "BUY_FILL_QTY_MISSING", reconciliationRequired: true, mayResend: false };

  const rawStopLimit = Number((Number(common.stop_loss_price) * 0.998).toPrecision(12));
  let normalized;
  try {
    normalized = await normalizeSpotProtection(
      common.symbol,
      qty,
      common.take_profit_price,
      common.stop_loss_price,
      rawStopLimit,
    );
  } catch (e) {
    return {
      ok: false,
      status: "PROTECTION_NORMALIZATION_FAILED",
      reconciliationRequired: true,
      mayResend: false,
      buy,
      reason: String(e?.message || e).slice(0, 120),
    };
  }
  const ids = clientIds(common.signal_id);
  const oco = await postSigned(env, OCO_URL, {
    ...common,
    ...ids,
    action: "OCO",
    quote_amount_usdt: 0,
    quantity: normalized.quantity,
    take_profit_price: normalized.takeProfit,
    stop_loss_price: normalized.stopLoss,
    stop_limit_price: normalized.stopLimit,
  });
  if (oco.unknown) return { ok: false, status: "OCO_STATUS_UNKNOWN", reconciliationRequired: true, mayResend: false, buy, clientIds: ids };
  if (!oco.transportOk || !["OCO_PLACED","PROTECTION_FAILED_EMERGENCY_CLOSED"].includes(String(oco.body?.status || ""))) {
    return { ok: false, status: oco.body?.status || "OCO_REJECTED", buy, oco, clientIds: ids };
  }
  return { ok: oco.body?.status === "OCO_PLACED", status: oco.body?.status, buy, oco, executedQty: qty, protectedQty: normalized.quantity, clientIds: ids, filters: { stepSize: normalized.stepSize, tickSize: normalized.tickSize, minNotional: normalized.minNotional } };
}
