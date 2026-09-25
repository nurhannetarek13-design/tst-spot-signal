import fs from "node:fs/promises";
import path from "node:path";
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

export function createV3ShadowReservationStore(initialRows = []) {
  const rows = new Map((initialRows || []).map(row => [row.key, structuredClone(row)]));
  return {
    async reserve(key, value = {}) {
      if (rows.has(key)) return { ok: false, existing: structuredClone(rows.get(key)) };
      const row = { key, ...value, status: "RESERVED", at: new Date().toISOString() };
      rows.set(key, row);
      return { ok: true, row: structuredClone(row) };
    },
    async update(key, value = {}) {
      const row = { ...(rows.get(key) || { key }), ...value, updatedAt: new Date().toISOString() };
      rows.set(key, row);
      return structuredClone(row);
    },
    async complete(key, value) {
      const row = { ...(rows.get(key) || { key }), ...value, status: "COMPLETED", updatedAt: new Date().toISOString() };
      rows.set(key, row);
      return structuredClone(row);
    },
    async fail(key, value) {
      const row = { ...(rows.get(key) || { key }), ...value, status: "FAILED", updatedAt: new Date().toISOString() };
      rows.set(key, row);
      return structuredClone(row);
    },
    async get(key) {
      const row = rows.get(key);
      return row ? structuredClone(row) : null;
    },
    async listPending() {
      return [...rows.values()]
        .filter(row => !["COMPLETED", "FAILED"].includes(String(row.status || "").toUpperCase()))
        .map(structuredClone);
    },
    async all() {
      return [...rows.values()].map(structuredClone);
    },
  };
}

export async function createV3FileReservationStore(filePath) {
  const target = path.resolve(String(filePath || ""));
  if (!target) throw new Error("RESERVATION_FILE_REQUIRED");
  let rows = [];
  try {
    const raw = JSON.parse(await fs.readFile(target, "utf8"));
    rows = Array.isArray(raw?.rows) ? raw.rows : [];
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const memory = createV3ShadowReservationStore(rows);

  async function persist() {
    const payload = JSON.stringify({ version: 1, rows: await memory.all() }, null, 2);
    await fs.mkdir(path.dirname(target), { recursive: true });
    const temp = target + ".tmp";
    await fs.writeFile(temp, payload, "utf8");
    await fs.rename(temp, target);
  }

  return {
    async reserve(key, value = {}) {
      const out = await memory.reserve(key, value);
      if (out.ok) await persist();
      return out;
    },
    async update(key, value = {}) {
      const out = await memory.update(key, value);
      await persist();
      return out;
    },
    async complete(key, value = {}) {
      const out = await memory.complete(key, value);
      await persist();
      return out;
    },
    async fail(key, value = {}) {
      const out = await memory.fail(key, value);
      await persist();
      return out;
    },
    get: key => memory.get(key),
    listPending: () => memory.listPending(),
    all: () => memory.all(),
    filePath: target,
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


export async function recoverV3ShadowReservations({
  exchange,
  reservations,
} = {}) {
  if (!exchange?.getOrder) throw new Error("RECOVERY_GET_ORDER_ADAPTER_REQUIRED");
  if (!reservations?.listPending) throw new Error("PERSISTENT_RESERVATION_STORE_REQUIRED");

  const pending = await reservations.listPending();
  const recovered = [];
  for (const row of pending) {
    const intent = row.intent;
    if (!intent) {
      await reservations.fail(row.key, { reason: "RECOVERY_INTENT_MISSING" });
      recovered.push({ key: row.key, status: "FAILED", reason: "RECOVERY_INTENT_MISSING" });
      continue;
    }
    assertV3IntentShadowOnly(intent);
    const ids = [...new Set(row.clientOrderIds || [])];
    const observed = [];
    for (const cid of ids) {
      try {
        let order = normalizeOrder(await exchange.getOrder({ symbol: intent.symbol, clientOrderId: cid }));
        if (!["FILLED", "CANCELED", "EXPIRED", "REJECTED"].includes(order.status)) {
          order = await cancelRemainder(exchange, intent.symbol, order);
        }
        observed.push(order);
      } catch {}
    }

    const combined = combineFills(observed);
    if (!(combined.executedQty > 0 && combined.averagePrice > 0)) {
      await reservations.fail(row.key, {
        reason: ids.length ? "RECOVERED_NO_FILL" : "RECOVERY_NO_ORDER_IDS",
        recoveredOrders: observed,
      });
      recovered.push({ key: row.key, status: "FAILED", reason: "RECOVERED_NO_FILL" });
      continue;
    }

    if (!exchange?.placeOcoSell) throw new Error("PROTECTION_ADAPTER_REQUIRED");
    const protection = await exchange.placeOcoSell({
      symbol: intent.symbol,
      quantity: combined.executedQty,
      stopPrice: intent.protection.stopPrice,
      takeProfitPrice: intent.protection.takeProfitPrice,
      clientTag: clientId(intent, 99),
    });
    if (!protection) throw new Error("RECOVERY_PROTECTION_NOT_CONFIRMED");

    const slippageBps = realizedSlippageBps(intent.entry.referencePrice, combined.averagePrice);
    await reservations.complete(row.key, {
      result: "RECOVERED_FILLED_AND_PROTECTED",
      recovered: true,
      recoveredOrders: observed,
      executedQty: combined.executedQty,
      averagePrice: combined.averagePrice,
      slippageBps,
      protection,
    });
    recovered.push({
      key: row.key,
      status: "RECOVERED",
      executedQty: combined.executedQty,
      averagePrice: combined.averagePrice,
      protection,
    });
  }
  return { ok: true, pendingCount: pending.length, recovered };
}

export async function executeV3ShadowIntent({
  intent,
  exchange,
  reservations = createV3ShadowReservationStore(),
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  pollMs = 250,
  maxRealizedSlippageBps = 12,
  now = () => Date.now(),
} = {}) {
  assertV3IntentShadowOnly(intent);
  if (!exchange) throw new Error("EXCHANGE_ADAPTER_REQUIRED");

  const currentMs=Number(now());
  const createdAtMs=Number(intent?.createdAtMs);
  const decisionLatencyMs=Number(intent?.decisionLatencyMs);
  const maxTotalLatencyMs=Number(intent?.maxTotalLatencyMs);
  if(!Number.isFinite(currentMs)||!Number.isFinite(createdAtMs)||
     !Number.isFinite(decisionLatencyMs)||!Number.isFinite(maxTotalLatencyMs)||
     decisionLatencyMs<0||maxTotalLatencyMs<=0){
    throw new Error("V3_LATENCY_CONTRACT_INVALID");
  }
  if(createdAtMs-currentMs>1000) throw new Error("EXECUTION_CLOCK_SKEW");
  const queueLatencyMs=Math.max(0,currentMs-createdAtMs);
  const totalLatencyMs=decisionLatencyMs+queueLatencyMs;
  if(totalLatencyMs>maxTotalLatencyMs){
    const error=new Error("STALE_EXECUTION_INTENT");
    error.totalLatencyMs=totalLatencyMs;
    error.maxTotalLatencyMs=maxTotalLatencyMs;
    throw error;
  }

  const key = executionIntentIdempotencyKey(intent);
  const reservation = await reservations.reserve(key, {
    intent: structuredClone(intent),
    clientOrderIds: [],
    stage: "RESERVED",
  });
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
      const cid = clientId(intent, 0);
      await reservations.update?.(key, { stage: "PLACING_ENTRY", clientOrderIds: [cid] });
      const order = await placeMarket({ exchange, intent, cid });
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
        const cid = clientId(intent, attempt);
        const existingReservation = await reservations.get?.(key);
        const knownIds = Array.isArray(existingReservation?.clientOrderIds) ? existingReservation.clientOrderIds : [];
        await reservations.update?.(key, {
          stage: "PLACING_ENTRY",
          clientOrderIds: [...new Set([...knownIds, cid])],
          attempt,
        });
        let order = await placeLimit({
          exchange,
          intent: localIntent,
          cid,
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
      const row = await reservations.complete(key, {
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

    const row = await reservations.complete(key, {
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
    await reservations.fail(key, { reason: String(error?.message || error) });
    throw error;
  }
}
