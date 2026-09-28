import { readLivePolicy } from "./live-cutover-policy.js";
import { normalizeSpotProtection } from "./binance-spot-filters.js";

function cleanId(value, prefix, max = 32) {
  const base = String(value || "sig").replace(/[^A-Za-z0-9]/g, "").slice(0, 20) || "sig";
  return (prefix + base).slice(0, max);
}

function clientIds(signalId) {
  return {
    buy: cleanId(signalId, "TSTB"),
    list: cleanId(signalId, "TSTO"),
    takeProfit: cleanId(signalId, "TSTT"),
    stop: cleanId(signalId, "TSTS"),
    emergency: cleanId(signalId, "TSTX"),
  };
}

function creds(env = {}) {
  const key = String(env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "").trim();
  const secret = String(env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || "").trim();
  return { key, secret };
}

async function hmacHex(secret, text) {
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

async function signedQuery(secret, params = {}) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    q.set(key, String(value));
  }
  q.set("recvWindow", "5000");
  q.set("timestamp", String(Date.now()));
  const unsigned = q.toString();
  q.set("signature", await hmacHex(secret, unsigned));
  return q.toString();
}

function relayConfigured(env = {}) {
  return Boolean(String(env.SUPABASE_BINANCE_RELAY_URL || "").trim());
}

function isUnknownBinanceResponse(body = {}, httpStatus = 0) {
  const code = Number(body?.binanceCode);
  const status = Number(body?.upstreamHttpStatus || httpStatus || 0);
  if (status >= 500 || status === 418 || status === 429) return true;
  if ([-1000, -1001, -1006, -1007, -1008].includes(code)) return true;
  return String(body?.status || "") === "BINANCE_TRANSPORT_UNKNOWN";
}

function isNotFoundResponse(body = {}) {
  return [-2011, -2013, -2022].includes(Number(body?.binanceCode));
}

function isDefiniteReject(body = {}, httpStatus = 0) {
  const status = Number(body?.upstreamHttpStatus || httpStatus || 0);
  if (isUnknownBinanceResponse(body, status)) return false;
  return status >= 400 && status < 500;
}

async function relay(env, method, path, params = {}, timeoutMs = 15000) {
  const url = String(env.SUPABASE_BINANCE_RELAY_URL || "").trim();
  const pair = creds(env);
  if (!url) return { ok: false, status: "SUPABASE_RELAY_NOT_CONFIGURED", noRequestSent: true };
  if (!pair.key || !pair.secret) return { ok: false, status: "BINANCE_CREDENTIALS_MISSING", noRequestSent: true };

  const query = await signedQuery(pair.secret, params);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ method, path, apiKey: pair.key, query }),
      signal: controller.signal,
    });
    const body = await r.json().catch(() => ({}));
    return {
      ok: r.ok && body?.ok === true,
      httpStatus: r.status,
      status: String(body?.status || ("HTTP_" + r.status)),
      body,
      data: body?.data ?? null,
      unknown: isUnknownBinanceResponse(body, r.status),
      definiteReject: isDefiniteReject(body, r.status),
      notFound: isNotFoundResponse(body),
    };
  } catch (error) {
    return {
      ok: false,
      unknown: true,
      status: "SUPABASE_RELAY_TRANSPORT_UNKNOWN",
      reason: String(error?.message || error).slice(0, 120),
      mayResend: false,
    };
  } finally {
    clearTimeout(timer);
  }
}

async function queryOrder(env, symbol, clientId) {
  const r = await relay(env, "GET", "/api/v3/order", { symbol, origClientOrderId: clientId });
  if (r.ok) return { found: true, order: r.data, response: r };
  if (r.notFound) return { found: false, confirmedAbsent: true, response: r };
  return { found: false, confirmedAbsent: false, unknown: true, response: r };
}

async function queryOrderList(env, listClientOrderId) {
  const r = await relay(env, "GET", "/api/v3/orderList", { origClientOrderId: listClientOrderId });
  if (r.ok) return { found: true, list: r.data, response: r };
  if (r.notFound) return { found: false, confirmedAbsent: true, response: r };
  return { found: false, confirmedAbsent: false, unknown: true, response: r };
}

async function queryWithRetries(fn, attempts = 5) {
  let last = null;
  let absentCount = 0;
  for (let i = 0; i < attempts; i++) {
    last = await fn();
    if (last?.found) return { ...last, reconciled: true };
    if (last?.confirmedAbsent) absentCount++;
    else absentCount = 0;
    if (i < attempts - 1) await new Promise((resolve) => setTimeout(resolve, 250 * (i + 1)));
  }
  return {
    ...(last || {}),
    reconciled: true,
    confirmedAbsent: absentCount === attempts,
  };
}

function buySummary(order, symbol) {
  const grossQty = Number(order?.executedQty || 0);
  const quoteSpent = Number(order?.cummulativeQuoteQty || order?.cumulativeQuoteQty || 0);
  const baseAsset = String(symbol || "").replace(/USDT$/i, "");
  const fills = Array.isArray(order?.fills) ? order.fills : [];
  const baseCommission = fills
    .filter((fill) => String(fill?.commissionAsset || "") === baseAsset)
    .reduce((sum, fill) => sum + Number(fill?.commission || 0), 0);
  const netQty = Math.max(0, grossQty - baseCommission);
  return {
    grossQty,
    netQty,
    quoteSpent,
    weightedPrice: grossQty > 0 ? quoteSpent / grossQty : 0,
    baseAsset,
    commissions: fills.map((fill) => ({
      asset: fill?.commissionAsset || null,
      commission: Number(fill?.commission || 0),
    })),
  };
}

async function placeBuy(env, { signalId, symbol, quoteUSDT }) {
  const ids = clientIds(signalId);
  const existing = await queryOrder(env, symbol, ids.buy);
  if (existing.found) {
    const state = String(existing.order?.status || "");
    if (state === "FILLED") return { ok: true, status: "BUY_FILLED", order: existing.order, ids, recovered: true };
    return {
      ok: false,
      status: "BUY_ALREADY_EXISTS_NONFINAL",
      reconciliationRequired: true,
      mayResend: false,
      ids,
      order: existing.order,
    };
  }
  if (!existing.confirmedAbsent) {
    return { ok: false, status: "BUY_PRECHECK_UNKNOWN", reconciliationRequired: true, mayResend: false, ids };
  }

  const placed = await relay(env, "POST", "/api/v3/order", {
    symbol,
    side: "BUY",
    type: "MARKET",
    quoteOrderQty: Number(quoteUSDT).toFixed(2),
    newClientOrderId: ids.buy,
    newOrderRespType: "FULL",
  });

  if (placed.ok) return { ok: true, status: "BUY_FILLED", order: placed.data, ids };

  if (placed.unknown) {
    const rec = await queryWithRetries(() => queryOrder(env, symbol, ids.buy));
    if (rec.found) {
      const state = String(rec.order?.status || "");
      if (state === "FILLED") return { ok: true, status: "BUY_FILLED", order: rec.order, ids, recovered: true };
      return {
        ok: false,
        status: "BUY_EXISTS_AFTER_UNKNOWN",
        reconciliationRequired: true,
        mayResend: false,
        ids,
        order: rec.order,
      };
    }
    return {
      ok: false,
      status: "BUY_SUBMISSION_UNKNOWN",
      reconciliationRequired: true,
      mayResend: false,
      ids,
      reconciliationConfirmedAbsent: rec.confirmedAbsent === true,
    };
  }

  return {
    ok: false,
    status: placed.status || "BUY_REJECTED",
    noOrderSent: placed.definiteReject === true,
    reconciliationRequired: placed.definiteReject !== true,
    mayResend: false,
    ids,
    response: placed,
  };
}

async function emergencyClose(env, { signalId, symbol, quantity }) {
  const ids = clientIds(signalId);
  const existing = await queryOrder(env, symbol, ids.emergency);
  if (existing.found) {
    const state = String(existing.order?.status || "");
    if (state === "FILLED") {
      return { ok: true, status: "PROTECTION_FAILED_EMERGENCY_CLOSED", emergencyOrder: existing.order, recovered: true };
    }
    return {
      ok: false,
      status: "EMERGENCY_CLOSE_EXISTS_NONFINAL",
      reconciliationRequired: true,
      mayResend: false,
      emergencyOrder: existing.order,
    };
  }
  if (!existing.confirmedAbsent) {
    return { ok: false, status: "EMERGENCY_CLOSE_PRECHECK_UNKNOWN", reconciliationRequired: true, mayResend: false };
  }

  const sent = await relay(env, "POST", "/api/v3/order", {
    symbol,
    side: "SELL",
    type: "MARKET",
    quantity,
    newClientOrderId: ids.emergency,
    newOrderRespType: "FULL",
  });
  if (sent.ok) {
    return { ok: true, status: "PROTECTION_FAILED_EMERGENCY_CLOSED", emergencyOrder: sent.data };
  }

  const rec = await queryWithRetries(() => queryOrder(env, symbol, ids.emergency));
  if (rec.found && String(rec.order?.status || "") === "FILLED") {
    return { ok: true, status: "PROTECTION_FAILED_EMERGENCY_CLOSED", emergencyOrder: rec.order, recovered: true };
  }
  return {
    ok: false,
    status: sent.unknown ? "EMERGENCY_CLOSE_UNKNOWN" : (sent.status || "EMERGENCY_CLOSE_REJECTED"),
    reconciliationRequired: true,
    mayResend: false,
  };
}

async function placeOco(env, { signalId, symbol, quantity, takeProfit, stopLoss, stopLimit }) {
  const ids = clientIds(signalId);
  const existing = await queryOrderList(env, ids.list);
  if (existing.found) {
    return { ok: true, status: "OCO_PLACED", orderList: existing.list, ids, recovered: true };
  }
  if (!existing.confirmedAbsent) {
    return { ok: false, status: "OCO_PRECHECK_UNKNOWN", reconciliationRequired: true, mayResend: false, ids };
  }

  const sent = await relay(env, "POST", "/api/v3/orderList/oco", {
    symbol,
    side: "SELL",
    quantity,
    listClientOrderId: ids.list,
    aboveType: "LIMIT_MAKER",
    abovePrice: takeProfit,
    aboveClientOrderId: ids.takeProfit,
    belowType: "STOP_LOSS_LIMIT",
    belowStopPrice: stopLoss,
    belowPrice: stopLimit,
    belowClientOrderId: ids.stop,
    belowTimeInForce: "GTC",
    newOrderRespType: "RESULT",
  });

  if (sent.ok) return { ok: true, status: "OCO_PLACED", orderList: sent.data, ids };

  const rec = await queryWithRetries(() => queryOrderList(env, ids.list));
  if (rec.found) {
    return { ok: true, status: "OCO_PLACED", orderList: rec.list, ids, recovered: true };
  }

  if (sent.unknown && !rec.confirmedAbsent) {
    return { ok: false, status: "OCO_SUBMISSION_UNKNOWN", reconciliationRequired: true, mayResend: false, ids };
  }

  if (!sent.definiteReject && !rec.confirmedAbsent) {
    return {
      ok: false,
      status: sent.status || "OCO_RECONCILIATION_REQUIRED",
      reconciliationRequired: true,
      mayResend: false,
      ids,
    };
  }

  return {
    ok: false,
    status: sent.definiteReject ? "OCO_DEFINITE_REJECT" : "OCO_CONFIRMED_ABSENT_AFTER_UNKNOWN",
    definiteReject: sent.definiteReject === true,
    reconciliationConfirmedAbsent: rec.confirmedAbsent === true,
    ids,
    response: sent,
  };
}

export function supabaseExecutionConfigured(env = {}) {
  const pair = creds(env);
  return relayConfigured(env)
    && Boolean(pair.key && pair.secret)
    && String(env.SUPABASE_EXECUTOR_READY || "").toLowerCase() === "true";
}

export async function supabaseReadOnlyHeartbeat(env) {
  const r = await relay(env, "GET", "/api/v3/account", { omitZeroBalances: "true" }, 15000);
  return {
    transportOk: r.ok === true,
    httpStatus: Number(r.httpStatus || 0),
    body: {
      status: r.ok ? "SUPABASE_V2_HEARTBEAT_OK" : (r.status || "SUPABASE_V2_HEARTBEAT_FAILED"),
      routeVersion: "v2",
      routeId: "SUPABASE_BINANCE_RELAY_V2",
      canTrade: r.data?.canTrade === true,
      accountType: r.data?.accountType || null,
      financialAction: false,
    },
    raw: r,
  };
}

export async function manualBuyAndProtectViaSupabase(env, input) {
  const policy = readLivePolicy(env);
  if (policy.liveExecutionEnabled !== true) {
    return { ok: false, status: "LIVE_EXECUTION_DISABLED", noOrderSent: true };
  }
  if (!supabaseExecutionConfigured(env)) {
    return { ok: false, status: "SUPABASE_EXECUTOR_NOT_READY", noOrderSent: true };
  }

  const quote = Math.min(Number(input.quote_amount_usdt || 0), Number(policy.maxOrderUSDT));
  if (!(quote >= 5 && quote <= 5.5)) return { ok: false, status: "ORDER_SIZE_BLOCKED", noOrderSent: true };

  const signalId = String(input.signal_id || "");
  const symbol = String(input.symbol || "").toUpperCase();
  const target = Number(input.take_profit_price || 0);
  const stop = Number(input.stop_loss_price || 0);
  if (!/^[A-Z0-9]{2,20}USDT$/.test(symbol) || !(target > stop && stop > 0)) {
    return { ok: false, status: "INVALID_EXECUTION_INPUT", noOrderSent: true };
  }

  const buy = await placeBuy(env, { signalId, symbol, quoteUSDT: quote });
  if (!buy.ok) return buy;

  const summary = buySummary(buy.order, symbol);
  if (!(summary.netQty > 0 && summary.quoteSpent > 0)) {
    return { ok: false, status: "BUY_FILL_QTY_MISSING", reconciliationRequired: true, mayResend: false, buy };
  }

  let normalized;
  try {
    normalized = await normalizeSpotProtection(
      symbol,
      summary.netQty,
      target,
      stop,
      Number((stop * 0.998).toPrecision(12)),
    );
  } catch (error) {
    return {
      ok: false,
      status: "PROTECTION_NORMALIZATION_FAILED",
      reconciliationRequired: true,
      mayResend: false,
      buy,
      reason: String(error?.message || error).slice(0, 120),
    };
  }

  const oco = await placeOco(env, {
    signalId,
    symbol,
    quantity: normalized.quantity,
    takeProfit: normalized.takeProfit,
    stopLoss: normalized.stopLoss,
    stopLimit: normalized.stopLimit,
  });

  if (oco.ok) {
    return {
      ok: true,
      status: "OCO_PLACED",
      buy: { body: {
        status: "BUY_FILLED",
        order_id: buy.order?.orderId ?? null,
        executed_qty: summary.netQty,
        gross_executed_qty: summary.grossQty,
        quote_spent: summary.quoteSpent,
        weighted_price: summary.weightedPrice,
        commissions: summary.commissions,
      }},
      oco: { body: {
        status: "OCO_PLACED",
        oco_order_list_id: oco.orderList?.orderListId ?? null,
      }},
      executedQty: summary.netQty,
      protectedQty: normalized.quantity,
      clientIds: oco.ids,
      filters: {
        stepSize: normalized.stepSize,
        tickSize: normalized.tickSize,
        minNotional: normalized.minNotional,
      },
    };
  }

  if (oco.status === "OCO_SUBMISSION_UNKNOWN" || oco.status === "OCO_PRECHECK_UNKNOWN") {
    return {
      ok: false,
      status: oco.status,
      reconciliationRequired: true,
      mayResend: false,
      buy,
      oco,
      clientIds: oco.ids,
    };
  }

  const safeToClose = oco.definiteReject === true || oco.reconciliationConfirmedAbsent === true;
  if (!safeToClose) {
    return {
      ok: false,
      status: oco.status || "OCO_RECONCILIATION_REQUIRED",
      reconciliationRequired: true,
      mayResend: false,
      buy,
      oco,
      clientIds: oco.ids,
    };
  }

  const closed = await emergencyClose(env, { signalId, symbol, quantity: normalized.quantity });
  if (!closed.ok) {
    return {
      ok: false,
      status: closed.status || "UNPROTECTED_POSITION",
      reconciliationRequired: true,
      mayResend: false,
      buy,
      oco,
      emergency: closed,
      clientIds: oco.ids,
    };
  }

  return {
    ok: true,
    status: "PROTECTION_FAILED_EMERGENCY_CLOSED",
    buy: { body: {
      status: "BUY_FILLED",
      order_id: buy.order?.orderId ?? null,
      executed_qty: summary.netQty,
      gross_executed_qty: summary.grossQty,
      quote_spent: summary.quoteSpent,
      weighted_price: summary.weightedPrice,
      commissions: summary.commissions,
    }},
    oco: { body: {
      status: "PROTECTION_FAILED_EMERGENCY_CLOSED",
      emergency_order_id: closed.emergencyOrder?.orderId ?? null,
    }},
    executedQty: summary.netQty,
    protectedQty: 0,
    clientIds: oco.ids,
  };
}
