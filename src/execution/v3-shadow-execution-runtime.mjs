import {
  assertV3IntentShadowOnly,
  executionIntentIdempotencyKey,
} from "./v3-execution-intent.mjs";

function finite(value, fallback = null) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}
function positive(value, name) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) throw new Error(name + "_INVALID");
  return n;
}
function normalizeOrder(order = {}) {
  return {
    orderId: order.orderId ?? null,
    clientOrderId: String(order.clientOrderId || ""),
    status: String(order.status || "UNKNOWN").toUpperCase(),
    executedQty: Math.max(0, finite(order.executedQty ?? order.quantityFilled, 0)),
    cumulativeQuoteQty: Math.max(0, finite(order.cumulativeQuoteQty ?? order.quoteFilled, 0)),
    averagePrice: finite(order.averagePrice, null),
    price: finite(order.price, null),
  };
}
function averageFromOrder(order) {
  const o = normalizeOrder(order);
  if (o.averagePrice && o.averagePrice > 0) return o.averagePrice;
  if (o.executedQty > 0 && o.cumulativeQuoteQty > 0) return o.cumulativeQuoteQty / o.executedQty;
  return o.price && o.price > 0 ? o.price : null;
}
function clientId(intent, attempt = 0) {
  const raw = String(intent.signalId || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 20) || "signal";
  return ("v3-" + raw + "-" + attempt).slice(0, 36);
}
function combineFills(fills) {
  const valid = fills.filter(x => Number(x.executedQty) > 0);
  const qty = valid.reduce((s, x) => s + Number(x.executedQty), 0);
  const quote = valid.reduce((s, x) => {
    const q = Number(x.cumulativeQuoteQty || 0);
    if (q > 0) return s + q;
    const p = Number(averageFromOrder(x) || 0);
    return s + Number(x.executedQty || 0) * p;
  }, 0);
  return {
    executedQty: qty,
    cumulativeQuoteQty: quote,
    averagePrice: qty > 0 ? quote / qty : null,
  };
}

export function createV3ShadowReservationStore() {
  const rows = new Map();
  return {
    reserve(key) {
      if (rows.has(key)) return { ok: false, existing: structuredClone(rows.get(key)) };
      const row = { key, status: "RESERVED", at: new Date().toISOString() };
      rows.set(key, row);
      return { ok: true, row: structuredClone(row) };
    },
    complete(key, value) {
      const row = { ...(rows.get(key) || { key }), ...value, status: "COMPLETED" };
      rows.set(key, row);
      return structuredClone(row);
    },
    fail(key, value) {
      const row = { ...(rows.get(key) || { key }), ...value, status: "FAILED" };
      rows.set(key, row);
      return structuredClone(row);
    },
    get(key) {
      const row = rows.get(key);
      return row ? structuredClone(row) : null;
    },
  };
}

async function reconcileAfterAmbiguous({ exchange, symbol, clientOrderId, error }) {
  if (!exchange?.getOrder) throw error;
  try {
    const row = await exchange.getOrder({ symbol, clientOrderId });
    if (row) return normalizeOrder(row);
  } catch {}
  throw error;
}

async function placeMarket({ exchange, intent, cid }) {
  if (!exchange?.placeMarketBuy) throw new Error("MARKET_BUY_ADAPTER_REQUIRED");
  try {
    return normalizeOrder(await exchange.placeMarketBuy({
      symbol: intent.symbol,
      quoteAmountUsdt: intent.quoteAmountUsdt,
      referencePrice: intent.entry.referencePrice,
      clientOrderId: cid,
    }));
  } catch (error) {
    return await reconcileAfterAmbiguous({
      exchange, symbol: intent.symbol, clientOrderId: cid, error,
    });
  }
}

async function placeLimit({ exchange, intent, cid, price }) {
  if (!exchange?.placeLimitBuy) throw new Error("LIMIT_BUY_ADAPTER_REQUIRED");
  try {
    return normalizeOrder(await exchange.placeLimitBuy({
      symbol: intent.symbol,
      quoteAmountUsdt: intent.quoteAmountUsdt,
      price,
      clientOrderId: cid,
    }));
  } catch (error) {
    return await reconcileAfterAmbiguous({
      exchange, symbol: intent.symbol, clientOrderId: cid, error,
    });
  }
}

async function refreshOrder(exchange, symbol, order) {
  if (!exchange?.getOrder || !order?.clientOrderId) return normalizeOrder(order);
  try {
    return normalizeOrder(await exchange.getOrder({ symbol, clientOrderId: order.clientOrderId }));
  } catch {
    return normalizeOrder(order);
  }
}

async function cancelRemainder(exchange, symbol, order) {
  const o = normalizeOrder(order);
  if (["FILLED", "CANCELED", "EXPIRED", "REJECTED"].includes(o.status)) return o;
  if (!exchange?.cancelOrder) throw new Error("CANCEL_ORDER_ADAPTER_REQUIRED");
  try {
    const cancelled = await exchange.cancelOrder({ symbol, clientOrderId: o.clientOrderId });
    return normalizeOrder(cancelled || { ...o, status: "CANCELED" });
  } catch (error) {
    return await reconcileAfterAmbiguous({
      exchange, symbol, clientOrderId: o.clientOrderId, error,
    });
  }
}

function realizedSlippageBps(reference, average) {
  const ref = positive(reference, "REFERENCE_PRICE");
  const avg = positive(average, "AVERAGE_FILL_PRICE");
  return (avg / ref - 1) * 10000;
}

export async function executeV3ShadowIntent({
  intent,
  exchange,
  reservations = createV3ShadowReservationStore(),
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  pollMs = 250,
  maxRealizedSlippageBps = 12,
} = {}) {
  assertV3IntentShadowOnly(intent);
  if (!exchange) throw new Error("EXCHANGE_ADAPTER_REQUIRED");

  const key = executionIntentIdempotencyKey(intent);
  const reservation = reservations.reserve(key);
  if (!reservation.ok) {
    return {
      ok: true,
      duplicate: true,
      authorization: "SHADOW_ONLY",
      idempotencyKey: key,
      existing: reservation.existing,
    };
  }

  const fills = [];
  const attempts = [];
  try {
    if (intent.entry.style === "MARKET") {
      const order = await placeMarket({ exchange, intent, cid: clientId(intent, 0) });
      const reconciled = await refreshOrder(exchange, intent.symbol, order);
      attempts.push(reconciled);
      if (reconciled.executedQty > 0) fills.push(reconciled);
      if (reconciled.status !== "FILLED") {
        const finalOrder = await cancelRemainder(exchange, intent.symbol, reconciled);
        if (finalOrder.executedQty > reconciled.executedQty) {
          fills.length = 0;
          fills.push(finalOrder);
        }
      }
    } else if (intent.entry.style === "AGGRESSIVE_LIMIT") {
      const deadlineMs = Math.max(250, Number(intent.entry.cancelAfterMs || 1500));
      const replacements = Math.max(0, Math.trunc(Number(intent.entry.maxCancelReplace || 0)));
      const cap = positive(intent.entry.limitPrice, "LIMIT_PRICE");
      let remainingQuote = positive(intent.quoteAmountUsdt, "QUOTE_AMOUNT");
      let attempt = 0;

      while (attempt <= replacements && remainingQuote > 1e-9) {
        let price = cap;
        if (attempt > 0 && exchange?.getBookTicker) {
          const book = await exchange.getBookTicker({ symbol: intent.symbol });
          const ask = finite(book?.ask ?? book?.askPrice, null);
          if (ask && ask > 0) price = Math.min(cap, ask);
        }

        const localIntent = {
          ...intent,
          quoteAmountUsdt: remainingQuote,
        };
        let order = await placeLimit({
          exchange,
          intent: localIntent,
          cid: clientId(intent, attempt),
          price,
        });
        attempts.push(order);

        const started = Date.now();
        while (!["FILLED", "CANCELED", "EXPIRED", "REJECTED"].includes(order.status)) {
          if (Date.now() - started >= deadlineMs) break;
          await sleep(Math.min(pollMs, deadlineMs));
          order = await refreshOrder(exchange, intent.symbol, order);
          attempts[attempts.length - 1] = order;
        }

        order = await refreshOrder(exchange, intent.symbol, order);
        if (order.executedQty > 0) {
          fills.push(order);
          const q = order.cumulativeQuoteQty > 0
            ? order.cumulativeQuoteQty
            : order.executedQty * positive(averageFromOrder(order), "PARTIAL_FILL_PRICE");
          remainingQuote = Math.max(0, remainingQuote - q);
        }

        if (order.status === "FILLED" || remainingQuote <= 1e-9) break;

        const cancelled = await cancelRemainder(exchange, intent.symbol, order);
        attempts[attempts.length - 1] = cancelled;

        // If any partial fill occurred, stop chasing and protect what actually filled.
        if (cancelled.executedQty > 0 || order.executedQty > 0) {
          if (fills.length === 0 || fills.at(-1).clientOrderId !== cancelled.clientOrderId) {
            fills.push(cancelled);
          }
          break;
        }
        attempt += 1;
      }
    } else {
      throw new Error("V3_EXECUTION_STYLE_INVALID");
    }

    const combined = combineFills(fills);
    if (!(combined.executedQty > 0 && combined.averagePrice > 0)) {
      const row = reservations.complete(key, {
        result: "NO_FILL",
        attempts,
      });
      return {
        ok: false,
        duplicate: false,
        authorization: "SHADOW_ONLY",
        idempotencyKey: key,
        reason: "NO_FILL",
        attempts,
        reservation: row,
      };
    }

    const slippageBps = realizedSlippageBps(intent.entry.referencePrice, combined.averagePrice);
    const slippageBreach = slippageBps > Number(maxRealizedSlippageBps);

    if (!exchange?.placeOcoSell) throw new Error("PROTECTION_ADAPTER_REQUIRED");
    const protection = await exchange.placeOcoSell({
      symbol: intent.symbol,
      quantity: combined.executedQty,
      stopPrice: intent.protection.stopPrice,
      takeProfitPrice: intent.protection.takeProfitPrice,
      clientTag: clientId(intent, 99),
    });
    if (!protection) throw new Error("PROTECTION_NOT_CONFIRMED");

    const row = reservations.complete(key, {
      result: "FILLED_AND_PROTECTED",
      executedQty: combined.executedQty,
      averagePrice: combined.averagePrice,
      slippageBps,
      slippageBreach,
      protection,
    });

    return {
      ok: true,
      duplicate: false,
      authorization: "SHADOW_ONLY",
      liveTrading: false,
      idempotencyKey: key,
      fill: combined,
      attempts,
      realizedSlippageBps: slippageBps,
      slippageBreach,
      protection,
      reservation: row,
    };
  } catch (error) {
    reservations.fail(key, { reason: String(error?.message || error) });
    throw error;
  }
}
