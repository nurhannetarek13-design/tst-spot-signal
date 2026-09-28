import { readLivePolicy } from "./live-cutover-policy.js";
import { normalizeSpotProtection, validateSpotMarketBuy } from "./binance-spot-filters.js";
import {
  binanceCredentials,
  signingCredentialsReady,
  buildSignedBinanceQuery,
  computeServerTimeOffset,
  safeSigningDiagnostics,
  signRelayEnvelope,
} from "./binance-signing.js";

function stableHash(value) {
  const input = String(value || "");
  let a = 0x811c9dc5, b = 0x9e3779b9;
  for (let i = 0; i < input.length; i++) {
    const code = input.charCodeAt(i);
    a ^= code;
    a = Math.imul(a, 0x01000193) >>> 0;
    b ^= (code + i) >>> 0;
    b = Math.imul(b, 0x85ebca6b) >>> 0;
  }
  return a.toString(16).padStart(8, "0") + b.toString(16).padStart(8, "0");
}

function cleanId(value, prefix, max = 32) {
  const raw = String(value || "sig");
  const base = raw.replace(/[^A-Za-z0-9]/g, "").slice(0, 10) || "sig";
  return (prefix + base + stableHash(raw)).slice(0, max);
}

export function deterministicIntentId(signalId, symbol = "") {
  const raw = String(signalId || "") + "|" + String(symbol || "").toUpperCase();
  return "TSTI" + stableHash(raw);
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

function logExecution(event, fields = {}) {
  const safe = {
    at:Date.now(),
    event:String(event || ""),
    intentId:fields.intentId || null,
    symbol:fields.symbol || null,
    state:fields.state || null,
    binanceOrderId:fields.binanceOrderId ?? null,
    orderListId:fields.orderListId ?? null,
    latencyMs:fields.latencyMs ?? null,
    binanceCode:fields.binanceCode ?? null,
    reconciliationOutcome:fields.reconciliationOutcome || null,
    noSecretValuesExposed:true,
  };
  console.log(JSON.stringify(safe));
}

async function emitExecutionEvent(eventSink, event) {
  if (typeof eventSink !== "function") return;
  try { await eventSink(event); } catch {}
}

const TRADE_STATES = new Set([
  "SIGNAL_CREATED",
  "APPROVED",
  "ENTRY_SUBMITTING",
  "ENTRY_ACCEPTED",
  "PARTIALLY_FILLED",
  "FILLED",
  "PROTECTION_PENDING",
  "PROTECTED",
  "EXITED",
  "FAILED_SAFE",
]);

function stateStub(env) {
  const id = env.STATE_COORDINATOR.idFromName("global");
  return env.STATE_COORDINATOR.get(id);
}

async function getStateKey(env, key) {
  const r = await stateStub(env).fetch("https://state/get?key=" + encodeURIComponent(key));
  return r.ok ? await r.json() : null;
}

async function putStateKey(env, key, value, ttlMs = 90 * 24 * 60 * 60 * 1000) {
  await stateStub(env).fetch("https://state/put?key=" + encodeURIComponent(key), {
    method:"PUT",
    headers:{"content-type":"application/json"},
    body:JSON.stringify({value,expiresAt:Date.now()+ttlMs}),
  });
}

async function claimStateKey(env, key, value, ttlMs = 90 * 24 * 60 * 60 * 1000) {
  const r = await stateStub(env).fetch("https://state/claim?key=" + encodeURIComponent(key), {
    method:"POST",
    headers:{"content-type":"application/json"},
    body:JSON.stringify({value,expiresAt:Date.now()+ttlMs}),
  });
  return r.ok;
}

async function updateActiveIntentIndex(env, intentId, state, row = {}) {
  const key="live:active-intents";
  const current=await getStateKey(env,key);
  const rows=Array.isArray(current)?current.filter(Boolean):[];
  const filtered=rows.filter((x)=>String(x?.intentId||"")!==String(intentId));
  const activeState=["PROTECTED","PROTECTION_PENDING","FILLED","PARTIALLY_FILLED"].includes(String(state))
    || (String(state)==="FAILED_SAFE" && row?.unprotectedPosition===true);
  if (activeState) {
    filtered.push({intentId:String(intentId),state:String(state),updatedAt:Date.now()});
  }
  await putStateKey(env,key,filtered.slice(-20));
}

async function readTradeState(env, intentId) {
  const r = await stateStub(env).fetch("https://state/get?key=" + encodeURIComponent("trade-intent:" + intentId));
  return r.ok ? await r.json() : null;
}

async function writeTradeState(env, intentId, state, patch = {}) {
  if (!TRADE_STATES.has(state)) throw new Error("BAD_TRADE_STATE");
  const previous = await readTradeState(env, intentId);
  if (previous?.state === "EXITED" && state !== "EXITED") throw new Error("TRADE_ALREADY_EXITED");
  if (previous?.state === "FAILED_SAFE" && !["FAILED_SAFE","EXITED"].includes(state)) throw new Error("TRADE_FAILED_SAFE_LOCKED");
  const row = {
    ...(previous || {}),
    ...patch,
    intentId,
    state,
    updatedAt: Date.now(),
    createdAt: Number(previous?.createdAt || patch?.createdAt || Date.now()),
  };
  await stateStub(env).fetch("https://state/put?key=" + encodeURIComponent("trade-intent:" + intentId), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value: row, expiresAt: Date.now() + 90 * 24 * 60 * 60 * 1000 }),
  });
  await updateActiveIntentIndex(env,intentId,state,row);
  return row;
}

async function claimIntent(env, intentId, patch = {}) {
  const row = {
    value: {
      intentId,
      state: "SIGNAL_CREATED",
      createdAt: Date.now(),
      updatedAt: Date.now(),
      ...patch,
    },
    expiresAt: Date.now() + 90 * 24 * 60 * 60 * 1000,
  };
  const r = await stateStub(env).fetch("https://state/claim?key=" + encodeURIComponent("trade-intent:" + intentId), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(row),
  });
  return r.ok;
}


let serverTimeCache = { offsetMs: 0, serverTimeMs: 0, checkedAt: 0, roundTripMs: 0 };

function relayRegion(env = {}) {
  return String(env.SUPABASE_BINANCE_REGION || "eu-west-1").trim() || "eu-west-1";
}

function randomNonce() {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  let raw = "";
  for (const b of bytes) raw += String.fromCharCode(b);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function getBinanceServerTime(env, { force = false } = {}) {
  const now = Date.now();
  if (!force && serverTimeCache.checkedAt && now - serverTimeCache.checkedAt < 30_000) {
    return { ...serverTimeCache, cached: true };
  }
  const url = String(env.SUPABASE_BINANCE_RELAY_URL || "").trim();
  if (!url) throw new Error("SUPABASE_RELAY_NOT_CONFIGURED");
  const before = Date.now();
  const r = await fetch(url + "?probe=time&nonce=" + encodeURIComponent(randomNonce()), {
    method: "GET",
    headers: { "cache-control": "no-store", "x-region": relayRegion(env) },
    signal: AbortSignal.timeout(10_000),
  });
  const after = Date.now();
  const body = await r.json().catch(() => ({}));
  if (!r.ok || body?.ok !== true || !Number.isFinite(Number(body?.serverTime))) {
    throw new Error("BINANCE_SERVER_TIME_UNAVAILABLE:" + String(body?.status || ("HTTP_" + r.status)));
  }
  const timing = computeServerTimeOffset({
    localBeforeMs: before,
    localAfterMs: after,
    serverTimeMs: Number(body.serverTime),
  });
  serverTimeCache = {
    ...timing,
    serverTimeMs: Number(body.serverTime),
    checkedAt: after,
  };
  return { ...serverTimeCache, cached: false };
}

async function buildRelayAuth(pair, method, path, query) {
  if (!pair.ed25519PrivateKey) throw new Error("SUPABASE_RELAY_AUTH_KEY_MISSING");
  const relayTimestamp = Date.now();
  const relayNonce = randomNonce();
  const signed = await signRelayEnvelope(pair.ed25519PrivateKey, {
    relayTimestamp,
    relayNonce,
    method,
    path,
    apiKey: pair.apiKey,
    query,
  });
  return {
    relayTimestamp,
    relayNonce,
    relaySignature: signed.relaySignature,
  };
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

async function relay(env, method, path, params = {}, timeoutMs = 15000, signingModeOverride = null) {
  const url = String(env.SUPABASE_BINANCE_RELAY_URL || "").trim();
  const pair = binanceCredentials(env);
  if (signingModeOverride) pair.signingMode = String(signingModeOverride).toUpperCase() === "ED25519" ? "ED25519" : "HMAC";
  if (!url) return { ok: false, status: "SUPABASE_RELAY_NOT_CONFIGURED", noRequestSent: true };
  if (!signingCredentialsReady(pair)) return { ok: false, status: "BINANCE_CREDENTIALS_MISSING", noRequestSent: true };
  if (!pair.ed25519PrivateKey) return { ok: false, status: "SUPABASE_RELAY_AUTH_KEY_MISSING", noRequestSent: true };

  let timing;
  let signed;
  try {
    timing = await getBinanceServerTime(env);
    const timestampMs = Date.now() + Number(timing.offsetMs || 0);
    signed = await buildSignedBinanceQuery(pair, params, { timestampMs, recvWindow: 5000 });
  } catch (error) {
    return {
      ok: false,
      status: "BINANCE_SIGNING_PREP_FAILED",
      reason: String(error?.message || error).slice(0, 160),
      noRequestSent: true,
    };
  }

  let auth;
  try {
    auth = await buildRelayAuth(pair, method, path, signed.query);
  } catch (error) {
    return {
      ok:false,
      status:"SUPABASE_RELAY_AUTH_PREP_FAILED",
      reason:String(error?.message || error).slice(0,160),
      noRequestSent:true,
      diagnostics:safeSigningDiagnostics({
        endpoint:path,
        method,
        unsignedPayload:signed.unsignedPayload,
        timestampMs:signed.timestampMs,
        serverTimeMs:Number(timing.serverTimeMs || 0),
        signingMode:signed.signingMode,
      }),
    };
  }
  const diagnosticBase = safeSigningDiagnostics({
    endpoint: path,
    method,
    unsignedPayload: signed.unsignedPayload,
    timestampMs: signed.timestampMs,
    serverTimeMs: Number(timing.serverTimeMs || 0),
    signingMode: signed.signingMode,
  });

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const startedAt = Date.now();
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-region": relayRegion(env),
      },
      body: JSON.stringify({
        method,
        path,
        apiKey: pair.apiKey,
        query: signed.query,
        relayTimestamp: auth.relayTimestamp,
        relayNonce: auth.relayNonce,
        relaySignature: auth.relaySignature,
      }),
      signal: controller.signal,
    });
    const body = await r.json().catch(() => ({}));
    const diagnostics = {
      ...diagnosticBase,
      httpStatus:r.status,
      binanceCode:body?.binanceCode == null ? null : Number(body.binanceCode),
    };
    return {
      ok: r.ok && body?.ok === true,
      httpStatus: r.status,
      status: String(body?.status || ("HTTP_" + r.status)),
      body,
      data: body?.data ?? null,
      unknown: isUnknownBinanceResponse(body, r.status),
      definiteReject: isDefiniteReject(body, r.status),
      notFound: isNotFoundResponse(body),
      latencyMs: Date.now() - startedAt,
      diagnostics,
    };
  } catch (error) {
    return {
      ok: false,
      unknown: true,
      status: "SUPABASE_RELAY_TRANSPORT_UNKNOWN",
      reason: String(error?.message || error).slice(0, 120),
      mayResend: false,
      latencyMs: Date.now() - startedAt,
      diagnostics: diagnosticBase,
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

async function cancelUnresolvedEntry(env, symbol, clientId) {
  const sent = await relay(env, "DELETE", "/api/v3/order", {
    symbol,
    origClientOrderId: clientId,
  });
  if (sent.ok) return { ok:true, status:"ENTRY_CANCEL_ACCEPTED", order:sent.data };
  if (sent.notFound) return { ok:true, status:"ENTRY_CANCEL_NOT_FOUND", order:null };
  return {
    ok:false,
    status:sent.unknown ? "ENTRY_CANCEL_UNKNOWN" : (sent.status || "ENTRY_CANCEL_REJECTED"),
    reconciliationRequired:true,
    mayResend:false,
    response:sent,
  };
}

function terminalEntryStatus(status) {
  return ["FILLED","CANCELED","REJECTED","EXPIRED","EXPIRED_IN_MATCH"].includes(String(status || ""));
}

async function settleEntryOrder(env, symbol, clientId, firstOrder = null, attempts = 6) {
  let order = firstOrder;
  for (let i = 0; i < attempts; i++) {
    if (order && terminalEntryStatus(order.status)) {
      const executedQty = Number(order?.executedQty || 0);
      if (String(order.status) === "FILLED") {
        return { ok:true, status:"BUY_FILLED", order, partial:false };
      }
      if (executedQty > 0) {
        return { ok:true, status:"BUY_PARTIAL_FINAL", order, partial:true };
      }
      return { ok:false, status:"ENTRY_TERMINATED_WITHOUT_FILL", order, noPosition:true, mayResend:false };
    }
    if (i > 0 || !order) {
      const rec = await queryOrder(env, symbol, clientId);
      if (rec.found) order = rec.order;
      else if (!rec.confirmedAbsent) {
        return { ok:false, status:"ENTRY_STATUS_UNKNOWN", reconciliationRequired:true, mayResend:false };
      }
    }
    if (order && String(order.status) === "PARTIALLY_FILLED" && i >= 2) break;
    if (i < attempts - 1) await new Promise((resolve) => setTimeout(resolve, 250 * (i + 1)));
  }

  if (order && ["NEW","PARTIALLY_FILLED"].includes(String(order.status || ""))) {
    const canceled = await cancelUnresolvedEntry(env, symbol, clientId);
    if (!canceled.ok) return canceled;
    const final = await queryWithRetries(() => queryOrder(env, symbol, clientId), 4);
    if (!final.found) {
      return { ok:false, status:"ENTRY_CANCEL_RECONCILIATION_UNKNOWN", reconciliationRequired:true, mayResend:false };
    }
    const executedQty = Number(final.order?.executedQty || 0);
    if (executedQty > 0) {
      return {
        ok:true,
        status:String(final.order?.status || "") === "FILLED" ? "BUY_FILLED" : "BUY_PARTIAL_FINAL",
        order:final.order,
        partial:String(final.order?.status || "") !== "FILLED",
      };
    }
    return { ok:false, status:"ENTRY_CANCELED_WITHOUT_FILL", order:final.order, noPosition:true, mayResend:false };
  }

  return { ok:false, status:"ENTRY_FINAL_STATE_UNKNOWN", reconciliationRequired:true, mayResend:false, order };
}

async function preTradeMarketBuy(env, { symbol, quoteUSDT, referencePrice = null }) {
  const account = await relay(env, "GET", "/api/v3/account", { omitZeroBalances:"true" }, 15000);
  if (!account.ok) {
    return { ok:false, status:"PRETRADE_ACCOUNT_READ_FAILED", response:account };
  }
  const availableUSDT = freeBalance(account.data, "USDT");
  try {
    const validation = await validateSpotMarketBuy(symbol, quoteUSDT, availableUSDT, { referencePrice });
    return { ok:true, validation };
  } catch (error) {
    return { ok:false, status:String(error?.message || error).slice(0,160) || "PRETRADE_FILTER_REJECTED" };
  }
}

async function placeBuy(env, { intentId, signalId, symbol, quoteUSDT, quoteOrderQty, eventSink = null }) {
  const ids = clientIds(signalId);
  const existing = await queryOrder(env, symbol, ids.buy);
  if (existing.found) {
    const settled = await settleEntryOrder(env, symbol, ids.buy, existing.order);
    if (settled.ok) {
      await writeTradeState(env, intentId, settled.partial ? "PARTIALLY_FILLED" : "FILLED", {
        symbol,
        signalId,
        buyOrderId:settled.order?.orderId ?? null,
        buyClientOrderId:ids.buy,
        executedQty:Number(settled.order?.executedQty || 0),
        recovered:true,
      });
      return { ...settled, ids, recovered:true };
    }
    return { ...settled, ids };
  }
  if (!existing.confirmedAbsent) {
    return { ok:false, status:"BUY_PRECHECK_UNKNOWN", reconciliationRequired:true, mayResend:false, ids };
  }

  await writeTradeState(env, intentId, "ENTRY_SUBMITTING", {
    symbol,
    signalId,
    buyClientOrderId:ids.buy,
    quoteUSDT:Number(quoteUSDT),
  });

  await emitExecutionEvent(eventSink, { type:"BUY_SUBMITTED", intentId, symbol, quoteUSDT:Number(quoteUSDT) });
  logExecution("BUY_SUBMITTED",{intentId,symbol,state:"ENTRY_SUBMITTING"});
  const placed = await relay(env, "POST", "/api/v3/order", {
    symbol,
    side:"BUY",
    type:"MARKET",
    quoteOrderQty:String(quoteOrderQty),
    newClientOrderId:ids.buy,
    newOrderRespType:"FULL",
  });

  if (placed.ok) {
    await writeTradeState(env, intentId, "ENTRY_ACCEPTED", {
      symbol,
      signalId,
      buyOrderId:placed.data?.orderId ?? null,
      buyClientOrderId:ids.buy,
      exchangeEntryStatus:String(placed.data?.status || "UNKNOWN"),
      executedQty:Number(placed.data?.executedQty || 0),
      entryLatencyMs:Number(placed.latencyMs || 0),
    });
    const settled = await settleEntryOrder(env, symbol, ids.buy, placed.data);
    if (settled.ok) {
      await writeTradeState(env, intentId, settled.partial ? "PARTIALLY_FILLED" : "FILLED", {
        buyOrderId:settled.order?.orderId ?? placed.data?.orderId ?? null,
        executedQty:Number(settled.order?.executedQty || 0),
        exchangeEntryStatus:String(settled.order?.status || ""),
      });
      const evt={type:settled.partial?"BUY_PARTIAL_FILL":"BUY_FILLED",intentId,symbol,orderId:settled.order?.orderId??placed.data?.orderId??null,executedQty:Number(settled.order?.executedQty||0)};
      await emitExecutionEvent(eventSink,evt);
      logExecution(evt.type,{intentId,symbol,state:settled.partial?"PARTIALLY_FILLED":"FILLED",binanceOrderId:evt.orderId,latencyMs:Number(placed.latencyMs||0)});
      return { ...settled, ids };
    }
    return { ...settled, ids };
  }

  if (placed.unknown) {
    const rec = await queryWithRetries(() => queryOrder(env, symbol, ids.buy));
    if (rec.found) {
      const settled = await settleEntryOrder(env, symbol, ids.buy, rec.order);
      if (settled.ok) {
        await writeTradeState(env, intentId, settled.partial ? "PARTIALLY_FILLED" : "FILLED", {
          buyOrderId:settled.order?.orderId ?? null,
          buyClientOrderId:ids.buy,
          executedQty:Number(settled.order?.executedQty || 0),
          recovered:true,
        });
        return { ...settled, ids, recovered:true };
      }
      return { ...settled, ids };
    }
    return {
      ok:false,
      status:"BUY_SUBMISSION_UNKNOWN",
      reconciliationRequired:true,
      mayResend:false,
      ids,
      reconciliationConfirmedAbsent:rec.confirmedAbsent === true,
    };
  }

  return {
    ok:false,
    status:placed.status || "BUY_REJECTED",
    noOrderSent:placed.definiteReject === true,
    reconciliationRequired:placed.definiteReject !== true,
    mayResend:false,
    ids,
    response:placed,
  };
}

async function emergencyClose(env, { intentId, signalId, symbol, quantity, eventSink = null }) {
  const ids = clientIds(signalId);
  await emitExecutionEvent(eventSink,{type:"PROTECTION_FAILURE_CRITICAL",intentId,symbol,action:"EMERGENCY_CLOSE"});
  logExecution("PROTECTION_FAILURE_CRITICAL",{intentId,symbol,state:"FAILED_SAFE"});
  const existing = await queryOrder(env, symbol, ids.emergency);
  if (existing.found) {
    const state = String(existing.order?.status || "");
    if (state === "FILLED") {
      await writeTradeState(env, intentId, "FAILED_SAFE", {
        symbol,
        signalId,
        emergencyClosed:true,
        emergencyOrderId:existing.order?.orderId ?? null,
        executedQty:Number(existing.order?.executedQty || 0),
        recovered:true,
      });
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
    await writeTradeState(env, intentId, "FAILED_SAFE", {
      symbol,
      signalId,
      emergencyClosed:true,
      emergencyOrderId:sent.data?.orderId ?? null,
      executedQty:Number(sent.data?.executedQty || 0),
    });
    await emitExecutionEvent(eventSink,{type:"EMERGENCY_CLOSE_FILLED",intentId,symbol,orderId:sent.data?.orderId??null});
    logExecution("EMERGENCY_CLOSE_FILLED",{intentId,symbol,state:"FAILED_SAFE",binanceOrderId:sent.data?.orderId??null});
    return { ok: true, status: "PROTECTION_FAILED_EMERGENCY_CLOSED", emergencyOrder: sent.data };
  }

  const rec = await queryWithRetries(() => queryOrder(env, symbol, ids.emergency));
  if (rec.found && String(rec.order?.status || "") === "FILLED") {
    await writeTradeState(env, intentId, "FAILED_SAFE", {
      symbol,
      signalId,
      emergencyClosed:true,
      emergencyOrderId:rec.order?.orderId ?? null,
      executedQty:Number(rec.order?.executedQty || 0),
      recovered:true,
    });
    return { ok: true, status: "PROTECTION_FAILED_EMERGENCY_CLOSED", emergencyOrder: rec.order, recovered: true };
  }
  return {
    ok: false,
    status: sent.unknown ? "EMERGENCY_CLOSE_UNKNOWN" : (sent.status || "EMERGENCY_CLOSE_REJECTED"),
    reconciliationRequired: true,
    mayResend: false,
  };
}

async function placeOco(env, { intentId, signalId, symbol, quantity, takeProfit, stopLoss, stopLimit, eventSink = null }) {
  const ids = clientIds(signalId);
  await writeTradeState(env, intentId, "PROTECTION_PENDING", {
    symbol,
    signalId,
    protectedQty:String(quantity),
    listClientOrderId:ids.list,
    takeProfitClientOrderId:ids.takeProfit,
    stopClientOrderId:ids.stop,
  });
  const existing = await queryOrderList(env, ids.list);
  if (existing.found) {
    await writeTradeState(env, intentId, "PROTECTED", {
      orderListId:existing.list?.orderListId ?? null,
      recovered:true,
    });
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

  if (sent.ok) {
    await writeTradeState(env, intentId, "PROTECTED", {
      orderListId:sent.data?.orderListId ?? null,
      protectionLatencyMs:Number(sent.latencyMs || 0),
    });
    await emitExecutionEvent(eventSink,{type:"PROTECTION_INSTALLED",intentId,symbol,orderListId:sent.data?.orderListId??null,quantity:String(quantity),takeProfit:String(takeProfit),stopLoss:String(stopLoss)});
    logExecution("PROTECTION_INSTALLED",{intentId,symbol,state:"PROTECTED",orderListId:sent.data?.orderListId??null,latencyMs:Number(sent.latencyMs||0)});
    return { ok: true, status: "OCO_PLACED", orderList: sent.data, ids };
  }

  const rec = await queryWithRetries(() => queryOrderList(env, ids.list));
  if (rec.found) {
    await writeTradeState(env, intentId, "PROTECTED", {
      orderListId:rec.list?.orderListId ?? null,
      recovered:true,
    });
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
  const pair = binanceCredentials(env);
  return relayConfigured(env)
    && signingCredentialsReady(pair)
    && Boolean(pair.ed25519PrivateKey)
    && String(env.SUPABASE_EXECUTOR_READY || "").toLowerCase() === "true";
}

function botClientId(value) {
  return /^TST[A-Z][A-Za-z0-9_-]*$/.test(String(value || ""));
}

function recognizedBotClientId(value) {
  return /^(TSTB|TSTQ|TSTO|TSTC|TSTT|TSTS|TSTU|TSTV|TSTX|TSTW)[A-Za-z0-9_-]*$/.test(String(value || ""));
}

function freeBalance(accountData, asset) {
  const balances = Array.isArray(accountData?.balances) ? accountData.balances : [];
  const row = balances.find((x) => String(x?.asset || "") === String(asset || ""));
  const free = Number(row?.free || 0);
  return Number.isFinite(free) && free >= 0 ? free : 0;
}

export async function supabaseReadOnlyReconcile(env) {
  const [account, openOrders, openLists, restrictions, accountStatus] = await Promise.all([
    relay(env, "GET", "/api/v3/account", { omitZeroBalances: "true" }, 15000),
    relay(env, "GET", "/api/v3/openOrders", {}, 15000),
    relay(env, "GET", "/api/v3/openOrderList", {}, 15000),
    relay(env, "GET", "/sapi/v1/account/apiRestrictions", {}, 15000),
    relay(env, "GET", "/sapi/v1/account/status", {}, 15000),
  ]);

  const allReadsOk = [account, openOrders, openLists, restrictions, accountStatus].every((r) => r?.ok === true);
  if (!allReadsOk) {
    return {
      ok: false,
      status: "SUPABASE_RECONCILIATION_READ_FAILED",
      accountOk: account.ok === true,
      openOrdersOk: openOrders.ok === true,
      openOrderListsOk: openLists.ok === true,
      apiRestrictionsOk: restrictions.ok === true,
      accountStatusOk: accountStatus.ok === true,
      canTrade: account.data?.canTrade === true,
      noUnknownOrders: false,
      noUnprotectedPositions: false,
      openOrdersChecked: 0,
      botOpenOrders: 0,
      protectedOrderLists: 0,
      unknownBotOrders: 0,
      unprotectedBotOrders: 0,
      financialAction: false,
      diagnostics: {
        account: account.diagnostics || null,
        openOrders: openOrders.diagnostics || null,
        openOrderLists: openLists.diagnostics || null,
        restrictions: restrictions.diagnostics || null,
        accountStatus: accountStatus.diagnostics || null,
      },
    };
  }

  const rows = Array.isArray(openOrders.data) ? openOrders.data : [];
  const lists = Array.isArray(openLists.data) ? openLists.data : [];
  const botRows = rows.filter((row) => botClientId(row?.clientOrderId));
  const botLists = lists.filter((row) => botClientId(row?.listClientOrderId));
  const listByOrderListId = new Map(botLists.map((row) => [String(row?.orderListId), row]));

  let protectedOrderLists = 0;
  const validProtectedListIds = new Set();
  for (const list of botLists) {
    const listId = String(list?.orderListId);
    const childIds = Array.isArray(list?.orders)
      ? list.orders.map((x) => String(x?.clientOrderId || ""))
      : rows.filter((x) => String(x?.orderListId) === listId).map((x) => String(x?.clientOrderId || ""));
    const hasTp = childIds.some((id) => id.startsWith("TSTT") || id.startsWith("TSTU"));
    const hasStop = childIds.some((id) => id.startsWith("TSTS") || id.startsWith("TSTV"));
    if (hasTp && hasStop && /^(TSTO|TSTC)/.test(String(list?.listClientOrderId || ""))) {
      protectedOrderLists++;
      validProtectedListIds.add(listId);
    }
  }

  const unknownRows = botRows.filter((row) => !recognizedBotClientId(row?.clientOrderId));
  const unprotectedRows = botRows.filter((row) => {
    const id = String(row?.clientOrderId || "");
    if (id.startsWith("TSTT") || id.startsWith("TSTS") || id.startsWith("TSTU") || id.startsWith("TSTV")) {
      return !validProtectedListIds.has(String(row?.orderListId));
    }
    // A bot-owned entry/emergency market order must never remain open.
    return id.startsWith("TSTB") || id.startsWith("TSTQ") || id.startsWith("TSTX") || id.startsWith("TSTW");
  });
  const unknownLists = botLists.filter((row) => !/^(TSTO|TSTC)/.test(String(row?.listClientOrderId || "")));

  const permission = restrictions.data || {};
  const readingAllowed = permission.enableReading === true;
  const spotTradingPermission = permission.enableSpotAndMarginTrading === true;
  const prohibitedPermissionEnabled =
    permission.enableWithdrawals === true ||
    permission.enableFutures === true ||
    permission.enableMargin === true;

  const statusValue = String(accountStatus.data?.data ?? accountStatus.data ?? "").toLowerCase();
  const accountNormal = !statusValue || statusValue === "normal";
  const noUnknownOrders = unknownRows.length === 0 && unknownLists.length === 0;
  const noUnprotectedPositions = unprotectedRows.length === 0;
  const permissionSafe = readingAllowed && !prohibitedPermissionEnabled;
  const canTrade = account.data?.canTrade === true;
  const ok = canTrade && accountNormal && permissionSafe && noUnknownOrders && noUnprotectedPositions;

  const assets = Array.isArray(account.data?.balances)
    ? account.data.balances
        .filter((x) => Number(x?.free || 0) > 0 || Number(x?.locked || 0) > 0)
        .map((x) => String(x?.asset || ""))
        .filter(Boolean)
        .slice(0, 100)
    : [];

  return {
    ok,
    status: ok ? "SUPABASE_RECONCILIATION_OK" : "SUPABASE_RECONCILIATION_FAILED",
    canTrade,
    accountNormal,
    accountType: account.data?.accountType || null,
    readingAllowed,
    spotTradingPermission,
    prohibitedPermissionEnabled,
    noUnknownOrders,
    noUnprotectedPositions,
    openOrdersChecked: rows.length,
    openOrderListsChecked: lists.length,
    botOpenOrders: botRows.length,
    botOpenOrderLists: botLists.length,
    protectedOrderLists,
    unknownBotOrders: unknownRows.length + unknownLists.length,
    unprotectedBotOrders: unprotectedRows.length,
    botOpenSymbols:[...new Set(botRows.map((row)=>String(row?.symbol || "")).filter(Boolean))],
    unprotectedSymbols:[...new Set(unprotectedRows.map((row)=>String(row?.symbol || "")).filter(Boolean))],
    nonZeroBalanceAssetCount: assets.length,
    nonZeroBalanceAssets: assets,
    quoteBalanceAvailable: freeBalance(account.data, "USDT") > 0,
    financialAction: false,
    diagnostics: {
      account: account.diagnostics || null,
      openOrders: openOrders.diagnostics || null,
      openOrderLists: openLists.diagnostics || null,
      restrictions: restrictions.diagnostics || null,
      accountStatus: accountStatus.diagnostics || null,
    },
  };
}

export async function supabaseRelayReplaySelftest(env) {
  const url=String(env.SUPABASE_BINANCE_RELAY_URL||"").trim();
  const pair=binanceCredentials(env);
  if(!url||!signingCredentialsReady(pair)||!pair.ed25519PrivateKey){
    return {ok:false,status:"SUPABASE_RELAY_SELFTEST_NOT_CONFIGURED",financialAction:false};
  }
  let timing,signed,auth;
  try{
    timing=await getBinanceServerTime(env,{force:true});
    signed=await buildSignedBinanceQuery(pair,{omitZeroBalances:"true"},{timestampMs:Date.now()+Number(timing.offsetMs||0),recvWindow:5000});
    auth=await buildRelayAuth(pair,"GET","/api/v3/account",signed.query);
  }catch(error){
    return {ok:false,status:"SUPABASE_RELAY_SELFTEST_PREP_FAILED",reason:String(error?.message||error).slice(0,160),financialAction:false};
  }
  const payload=JSON.stringify({
    method:"GET",
    path:"/api/v3/account",
    apiKey:pair.apiKey,
    query:signed.query,
    relayTimestamp:auth.relayTimestamp,
    relayNonce:auth.relayNonce,
    relaySignature:auth.relaySignature,
  });
  const call=async()=>{
    try{
      const r=await fetch(url,{
        method:"POST",
        headers:{"content-type":"application/json","cache-control":"no-store","x-region":relayRegion(env)},
        body:payload,
        signal:AbortSignal.timeout(15_000),
      });
      const row=await r.json().catch(()=>({}));
      return {
        httpStatus:r.status,
        ok:row?.ok===true,
        status:String(row?.status||("HTTP_"+r.status)),
        binanceCode:row?.binanceCode??null,
        financialAction:row?.financialAction===true,
      };
    }catch(error){
      return {httpStatus:0,ok:false,status:"TRANSPORT_ERROR",reason:String(error?.name||"FetchError"),financialAction:false};
    }
  };
  const first=await call();
  const replay=await call();
  const passed=first.status!=="RELAY_REPLAY_BLOCKED"
    && replay.status==="RELAY_REPLAY_BLOCKED"
    && replay.httpStatus===401
    && first.financialAction!==true
    && replay.financialAction!==true;
  return {
    ok:passed,
    status:passed?"SUPABASE_RELAY_REPLAY_PROTECTION_OK":"SUPABASE_RELAY_REPLAY_PROTECTION_FAILED",
    first,
    replay,
    financialAction:false,
    noSecretValuesExposed:true,
  };
}

export async function supabaseSigningModeProbe(env) {
  const base=binanceCredentials(env);
  const modes=["HMAC","ED25519"];
  const results=[];
  for(const mode of modes){
    const candidate={...base,signingMode:mode};
    const credentialPresent=mode==="HMAC" ? Boolean(candidate.hmacSecret) : Boolean(candidate.ed25519PrivateKey);
    if(!candidate.apiKey || !credentialPresent){
      results.push({mode,ok:false,status:"SIGNING_CREDENTIAL_MISSING",httpStatus:null,binanceCode:null});
      continue;
    }
    const r=await relay(env,"GET","/api/v3/account",{omitZeroBalances:"true"},15000,mode);
    results.push({
      mode,
      ok:r.ok===true,
      status:r.status||null,
      httpStatus:Number(r.httpStatus||0)||null,
      binanceCode:r.body?.binanceCode ?? r.diagnostics?.binanceCode ?? null,
      diagnostics:r.diagnostics||null,
    });
    if(r.ok===true) break;
  }
  const winner=results.find((x)=>x.ok===true)||null;
  return {
    ok:Boolean(winner),
    status:winner?"BINANCE_SIGNING_MODE_PROVEN":"NO_VALID_BINANCE_SIGNING_MODE",
    signingMode:winner?.mode||null,
    results,
    financialAction:false,
    noSecretValuesExposed:true,
  };
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
      reason:r.reason || null,
      diagnostics:r.diagnostics || null,
    },
    raw: r,
  };
}

function sumTrades(trades = []) {
  const rows=Array.isArray(trades)?trades:[];
  const commissions={};
  let qty=0,quote=0;
  for(const t of rows){
    const q=Number(t?.qty||0),p=Number(t?.price||0),qq=Number(t?.quoteQty||0);
    qty+=Number.isFinite(q)?q:0;
    quote+=Number.isFinite(qq)&&qq>0?qq:(Number.isFinite(q*p)?q*p:0);
    const asset=String(t?.commissionAsset||"");
    const fee=Number(t?.commission||0);
    if(asset&&Number.isFinite(fee)) commissions[asset]=(commissions[asset]||0)+fee;
  }
  return {qty,quote,commissions};
}

async function tradesForOrder(env, symbol, orderId) {
  if (!(Number(orderId)>0)) return {ok:false,status:"ORDER_ID_MISSING",trades:[]};
  const r=await relay(env,"GET","/api/v3/myTrades",{symbol,orderId:Number(orderId)},15000);
  return {ok:r.ok===true,status:r.status,trades:Array.isArray(r.data)?r.data:[],response:r};
}

async function applyDailyRealizedPnlOnce(env,intentId,pnlUSDT) {
  const claimed=await claimStateKey(env,"daily-pnl-applied:"+intentId,{at:Date.now(),pnlUSDT},365*24*60*60*1000);
  if(!claimed) return {applied:false,status:"DAILY_PNL_ALREADY_APPLIED"};
  const day=new Date().toISOString().slice(0,10);
  const current=await getStateKey(env,"risk:daily-live");
  const sameDay=current?.day===day;
  const previousPnl=sameDay?Number(current?.realizedPnlUSDT||0):0;
  const nextPnl=previousPnl+Number(pnlUSDT||0);
  const row={
    day,
    realizedPnlUSDT:nextPnl,
    realizedLossUSDT:Math.max(0,-nextPnl),
    updatedAt:Date.now(),
  };
  await putStateKey(env,"risk:daily-live",row,3*24*60*60*1000);
  return {applied:true,status:"DAILY_PNL_APPLIED",...row};
}

export async function supabaseReconcileTradeByIntent(env, intentId) {
  const state=await readTradeState(env,intentId);
  if(!state) return {ok:false,status:"TRADE_INTENT_NOT_FOUND",intentId,financialAction:false};
  if(state.state==="EXITED") return {ok:true,status:"TRADE_ALREADY_EXITED",intentId,state,financialAction:false};
  if(!["PROTECTED","PROTECTION_PENDING","FILLED","PARTIALLY_FILLED","FAILED_SAFE"].includes(String(state.state||""))){
    return {ok:true,status:"TRADE_NOT_ACTIVE",intentId,state,financialAction:false};
  }

  const symbol=String(state.symbol||"").toUpperCase();
  if(!symbol) return {ok:false,status:"TRADE_SYMBOL_MISSING",intentId,financialAction:false};

  const listId=String(state.listClientOrderId||"");
  const tpId=String(state.takeProfitClientOrderId||"");
  const stopId=String(state.stopClientOrderId||"");
  const [list,tp,sl,account]=await Promise.all([
    listId?queryOrderList(env,listId):Promise.resolve({found:false,confirmedAbsent:true}),
    tpId?queryOrder(env,symbol,tpId):Promise.resolve({found:false,confirmedAbsent:true}),
    stopId?queryOrder(env,symbol,stopId):Promise.resolve({found:false,confirmedAbsent:true}),
    relay(env,"GET","/api/v3/account",{omitZeroBalances:"true"},15000),
  ]);

  if(list.unknown||tp.unknown||sl.unknown||!account.ok){
    logExecution("EXIT_RECONCILIATION_UNKNOWN",{intentId,symbol,state:state.state,reconciliationOutcome:"UNKNOWN"});
    return {ok:false,status:"EXIT_RECONCILIATION_UNKNOWN",intentId,reconciliationRequired:true,financialAction:false};
  }

  if(list.found && ["EXECUTING","RESPONSE"].includes(String(list.list?.listOrderStatus||""))){
    return {ok:true,status:"POSITION_STILL_PROTECTED",intentId,state:"PROTECTED",financialAction:false};
  }

  const tpFilled=tp.found && String(tp.order?.status||"")==="FILLED";
  const slFilled=sl.found && String(sl.order?.status||"")==="FILLED";
  let exitOrder=null,exitReason=null;
  if(tpFilled){ exitOrder=tp.order; exitReason="TAKE_PROFIT"; }
  else if(slFilled){ exitOrder=sl.order; exitReason="STOP_LOSS"; }

  const baseAsset=symbol.replace(/USDT$/,"");
  const balances=Array.isArray(account.data?.balances)?account.data.balances:[];
  const baseRow=balances.find((x)=>String(x?.asset||"")===baseAsset);
  const baseTotal=Number(baseRow?.free||0)+Number(baseRow?.locked||0);
  const trackedQty=Number(state.protectedQty||state.executedQty||0);

  if(state.state==="FAILED_SAFE" && state.unprotectedPosition===true){
    if(trackedQty>0 && baseTotal < trackedQty*0.2){
      await writeTradeState(env,intentId,"EXITED",{
        exitReason:"EXTERNAL_POSITION_CHANGE",
        exitedAt:Date.now(),
        exitOrderId:null,
        realizedPnlUSDT:null,
        reconciliationOutcome:"EXTERNAL_EXIT_DETECTED_AFTER_FAIL_SAFE",
      });
      logExecution("EXTERNAL_POSITION_CHANGE",{intentId,symbol,state:"EXITED",reconciliationOutcome:"EXTERNAL_EXIT_DETECTED_AFTER_FAIL_SAFE"});
      return {ok:true,status:"EXTERNAL_EXIT_DETECTED",intentId,symbol,exitReason:"EXTERNAL_POSITION_CHANGE",financialAction:false};
    }
    return {ok:false,status:"PROTECTION_LOST_POSITION_MAY_REMAIN",intentId,symbol,unprotectedPosition:true,reconciliationRequired:true,financialAction:false};
  }

  if(!exitOrder){
    const listDone=list.found && ["ALL_DONE","REJECT"].includes(String(list.list?.listOrderStatus||""));
    const allProtectionGone=(!tp.found||["CANCELED","EXPIRED","REJECTED"].includes(String(tp.order?.status||"")))
      &&(!sl.found||["CANCELED","EXPIRED","REJECTED"].includes(String(sl.order?.status||"")));
    if(listDone&&allProtectionGone){
      if(trackedQty>0 && baseTotal < trackedQty*0.2){
        await writeTradeState(env,intentId,"EXITED",{
          exitReason:"EXTERNAL_POSITION_CHANGE",
          exitedAt:Date.now(),
          exitOrderId:null,
          realizedPnlUSDT:null,
          reconciliationOutcome:"EXTERNAL_EXIT_DETECTED",
        });
        logExecution("EXTERNAL_POSITION_CHANGE",{intentId,symbol,state:"EXITED",reconciliationOutcome:"EXTERNAL_EXIT_DETECTED"});
        return {ok:true,status:"EXTERNAL_EXIT_DETECTED",intentId,exitReason:"EXTERNAL_POSITION_CHANGE",financialAction:false};
      }
      await writeTradeState(env,intentId,"FAILED_SAFE",{
        reason:"PROTECTION_GONE_POSITION_MAY_REMAIN",
        unprotectedPosition:true,
        baseBalanceObserved:baseTotal,
      });
      logExecution("PROTECTION_LOST",{intentId,symbol,state:"FAILED_SAFE",reconciliationOutcome:"UNPROTECTED"});
      return {ok:false,status:"PROTECTION_LOST_POSITION_MAY_REMAIN",intentId,unprotectedPosition:true,reconciliationRequired:true,financialAction:false};
    }
    return {ok:true,status:"POSITION_RECONCILED_OPEN",intentId,state:state.state,financialAction:false};
  }

  const [entryTrades,exitTrades]=await Promise.all([
    tradesForOrder(env,symbol,state.buyOrderId),
    tradesForOrder(env,symbol,exitOrder.orderId),
  ]);
  const entry=sumTrades(entryTrades.trades);
  const exit=sumTrades(exitTrades.trades);
  const feeAssets={...entry.commissions};
  for(const [asset,fee] of Object.entries(exit.commissions)) feeAssets[asset]=(feeAssets[asset]||0)+Number(fee||0);
  const usdtFees=Number(feeAssets.USDT||0);
  const realizedPnlUSDT=(exit.quote-entry.quote)-usdtFees;
  const nonQuoteFees=Object.fromEntries(Object.entries(feeAssets).filter(([asset])=>asset!=="USDT"));

  const lock=await claimStateKey(env,"exit-finalize:"+intentId,{at:Date.now(),exitOrderId:exitOrder.orderId},365*24*60*60*1000);
  if(!lock){
    const current=await readTradeState(env,intentId);
    return {ok:true,status:"EXIT_ALREADY_FINALIZED",intentId,state:current,financialAction:false};
  }

  const daily=await applyDailyRealizedPnlOnce(env,intentId,realizedPnlUSDT);
  await writeTradeState(env,intentId,"EXITED",{
    exitReason,
    exitedAt:Date.now(),
    exitOrderId:exitOrder.orderId,
    exitClientOrderId:exitOrder.clientOrderId||null,
    sellQty:exit.qty,
    exitQuote:exit.quote,
    entryQuote:entry.quote,
    realizedPnlUSDT,
    commissionByAsset:feeAssets,
    nonQuoteFees,
    dailyRiskAfterExit:daily,
    reconciliationOutcome:"EXIT_MATCHED_BINANCE_FILLS",
  });
  logExecution("TRADE_EXITED",{intentId,symbol,state:"EXITED",binanceOrderId:exitOrder.orderId,reconciliationOutcome:exitReason});
  return {
    ok:true,
    status:"TRADE_EXITED",
    intentId,
    symbol,
    exitReason,
    exitOrderId:exitOrder.orderId,
    realizedPnlUSDT,
    commissionByAsset:feeAssets,
    nonQuoteFees,
    dailyRisk:daily,
    financialAction:false,
  };
}

export async function supabaseReconcileActiveTrades(env) {
  const index=await getStateKey(env,"live:active-intents");
  const rows=Array.isArray(index)?index.filter(Boolean):[];
  const results=[];
  for(const row of rows.slice(0,20)){
    results.push(await supabaseReconcileTradeByIntent(env,String(row.intentId||"")));
  }
  return {
    ok:results.every((x)=>x?.ok===true),
    status:"ACTIVE_TRADE_RECONCILIATION_COMPLETE",
    checked:results.length,
    results,
    financialAction:false,
  };
}

export async function supabaseDryRunExecution(env, input) {
  const pair = binanceCredentials(env);
  if (!relayConfigured(env) || !signingCredentialsReady(pair) || !pair.ed25519PrivateKey) {
    return { ok:false, status:"SUPABASE_DRYRUN_NOT_CONFIGURED", financialAction:false };
  }

  const policy = readLivePolicy(env);
  const quote = Math.min(Number(input?.quote_amount_usdt || 0), Number(policy.maxOrderUSDT));
  const symbol = String(input?.symbol || "").toUpperCase();
  const signalId = String(input?.signal_id || "");
  const target = Number(input?.take_profit_price || 0);
  const stop = Number(input?.stop_loss_price || 0);
  const entry = Number(input?.entry_price || 0);
  if (!(quote >= 5 && quote <= 5.5)) return { ok:false, status:"ORDER_SIZE_BLOCKED", financialAction:false };
  if (!/^[A-Z0-9]{2,20}USDT$/.test(symbol) || !(target > stop && stop > 0)) {
    return { ok:false, status:"INVALID_EXECUTION_INPUT", financialAction:false };
  }

  const reconciliation = await supabaseReadOnlyReconcile(env);
  if (!reconciliation.ok) {
    return {
      ok:false,
      status:"DRYRUN_RECONCILIATION_BLOCKED",
      reconciliation,
      financialAction:false,
    };
  }

  const account = await relay(env, "GET", "/api/v3/account", { omitZeroBalances:"true" }, 15000);
  if (!account.ok) {
    return { ok:false, status:"DRYRUN_ACCOUNT_READ_FAILED", diagnostics:account.diagnostics || null, financialAction:false };
  }
  const availableUSDT = freeBalance(account.data, "USDT");

  let validation;
  try {
    validation = await validateSpotMarketBuy(symbol, quote, availableUSDT, {
      referencePrice: entry > 0 ? entry : null,
    });
  } catch (error) {
    return {
      ok:false,
      status:"DRYRUN_PRETRADE_REJECTED",
      reason:String(error?.message || error).slice(0,160),
      financialAction:false,
    };
  }

  const ids = clientIds(signalId);
  const buyParams = {
    symbol,
    side:"BUY",
    type:"MARKET",
    quoteOrderQty:validation.quoteOrderQty,
    newClientOrderId:ids.buy,
    newOrderRespType:"FULL",
  };

  const test = await relay(env, "POST", "/api/v3/order/test", buyParams, 15000);
  if (!test.ok) {
    return {
      ok:false,
      status:"BINANCE_ORDER_TEST_REJECTED",
      binanceCode:test.body?.binanceCode ?? null,
      httpStatus:test.httpStatus || null,
      diagnostics:test.diagnostics || null,
      validation,
      reconciliation,
      financialAction:false,
    };
  }

  let protection;
  try {
    protection = await normalizeSpotProtection(
      symbol,
      validation.estimatedBaseQty,
      target,
      stop,
      Number((stop * 0.998).toPrecision(12)),
    );
  } catch (error) {
    return {
      ok:false,
      status:"DRYRUN_PROTECTION_REJECTED",
      reason:String(error?.message || error).slice(0,160),
      validation,
      reconciliation,
      financialAction:false,
    };
  }

  const wouldSend = {
    intentId:deterministicIntentId(signalId, symbol),
    buy:{
      method:"POST",
      endpoint:"/api/v3/order",
      params:buyParams,
    },
    protection:{
      method:"POST",
      endpoint:"/api/v3/orderList/oco",
      params:{
        symbol,
        side:"SELL",
        quantity:protection.quantity,
        listClientOrderId:ids.list,
        aboveType:"LIMIT_MAKER",
        abovePrice:protection.takeProfit,
        aboveClientOrderId:ids.takeProfit,
        belowType:"STOP_LOSS_LIMIT",
        belowStopPrice:protection.stopLoss,
        belowPrice:protection.stopLimit,
        belowClientOrderId:ids.stop,
        belowTimeInForce:"GTC",
        newOrderRespType:"RESULT",
      },
    },
  };

  return {
    ok:true,
    status:"SUPABASE_V2_PRODUCTION_DRYRUN_PASS",
    route:"CLOUDFLARE_SIGNED_SUPABASE_BINANCE",
    binanceOrderTestPassed:true,
    writesEnabled:false,
    liveExecutionEnabled:policy.liveExecutionEnabled === true,
    autonomousEnabled:policy.autonomousEnabled === true,
    validation,
    reconciliation,
    wouldSend,
    diagnostics:test.diagnostics || null,
    financialAction:false,
  };
}

export async function manualBuyAndProtectViaSupabase(env, input) {
  const policy = readLivePolicy(env);
  const eventSink = typeof input?.onEvent === "function" ? input.onEvent : null;
  if (policy.liveExecutionEnabled !== true) {
    return { ok:false, status:"LIVE_EXECUTION_DISABLED", noOrderSent:true };
  }
  if (!supabaseExecutionConfigured(env)) {
    return { ok:false, status:"SUPABASE_EXECUTOR_NOT_READY", noOrderSent:true };
  }

  const quote = Math.min(Number(input.quote_amount_usdt || 0), Number(policy.maxOrderUSDT));
  if (!(quote >= 5 && quote <= 5.5)) return { ok:false, status:"ORDER_SIZE_BLOCKED", noOrderSent:true };

  const signalId = String(input.signal_id || "");
  const symbol = String(input.symbol || "").toUpperCase();
  const target = Number(input.take_profit_price || 0);
  const stop = Number(input.stop_loss_price || 0);
  const entry = Number(input.entry_price || 0);
  if (!/^[A-Z0-9]{2,20}USDT$/.test(symbol) || !(target > stop && stop > 0)) {
    return { ok:false, status:"INVALID_EXECUTION_INPUT", noOrderSent:true };
  }

  const intentId = deterministicIntentId(signalId, symbol);
  const claimed = await claimIntent(env, intentId, { signalId, symbol });
  if (!claimed) {
    const existing = await readTradeState(env, intentId);
    if (existing?.state === "EXITED") {
      return { ok:false, status:"TRADE_INTENT_ALREADY_EXITED", noOrderSent:true, intentId };
    }
    if (existing?.state === "FAILED_SAFE" && existing?.emergencyClosed !== true) {
      return { ok:false, status:"TRADE_INTENT_FAILED_SAFE_LOCKED", noOrderSent:true, intentId };
    }
  }
  await writeTradeState(env, intentId, "APPROVED", {
    signalId,
    symbol,
    quoteUSDT:quote,
    target,
    stop,
  });

  const reconciliation = await supabaseReadOnlyReconcile(env);
  if (!reconciliation.ok || !reconciliation.noUnknownOrders || !reconciliation.noUnprotectedPositions) {
    await writeTradeState(env, intentId, "FAILED_SAFE", {
      reason:"PRE_ENTRY_RECONCILIATION_BLOCKED",
      reconciliationStatus:reconciliation.status,
    });
    return {
      ok:false,
      status:"PRE_ENTRY_RECONCILIATION_BLOCKED",
      reconciliationRequired:true,
      mayResend:false,
      intentId,
      reconciliation,
    };
  }
  if (reconciliation.spotTradingPermission !== true) {
    await writeTradeState(env, intentId, "FAILED_SAFE", { reason:"SPOT_API_PERMISSION_REQUIRED" });
    return { ok:false, status:"SPOT_API_PERMISSION_REQUIRED", noOrderSent:true, intentId };
  }

  const preTrade = await preTradeMarketBuy(env, {
    symbol,
    quoteUSDT:quote,
    referencePrice:entry > 0 ? entry : null,
  });
  if (!preTrade.ok) {
    await writeTradeState(env, intentId, "FAILED_SAFE", { reason:preTrade.status });
    return { ok:false, status:preTrade.status, noOrderSent:true, intentId };
  }

  const buy = await placeBuy(env, {
    intentId,
    signalId,
    symbol,
    quoteUSDT:quote,
    quoteOrderQty:preTrade.validation.quoteOrderQty,
    eventSink,
  });
  if (!buy.ok) {
    if (buy.noPosition === true || buy.noOrderSent === true) {
      await writeTradeState(env, intentId, "FAILED_SAFE", { reason:buy.status, noPosition:true });
    }
    return { ...buy, intentId, preTrade:preTrade.validation };
  }

  const summary = buySummary(buy.order, symbol);
  if (!(summary.netQty > 0 && summary.quoteSpent > 0)) {
    await writeTradeState(env, intentId, "FAILED_SAFE", { reason:"BUY_FILL_QTY_MISSING" });
    return {
      ok:false,
      status:"BUY_FILL_QTY_MISSING",
      reconciliationRequired:true,
      mayResend:false,
      buy,
      intentId,
    };
  }

  await writeTradeState(env, intentId, buy.partial ? "PARTIALLY_FILLED" : "FILLED", {
    buyOrderId:buy.order?.orderId ?? null,
    executedQty:summary.netQty,
    grossExecutedQty:summary.grossQty,
    quoteSpent:summary.quoteSpent,
    weightedPrice:summary.weightedPrice,
    partialFill:buy.partial === true,
  });

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
    await writeTradeState(env, intentId, "PROTECTION_PENDING", {
      reason:"PROTECTION_NORMALIZATION_FAILED",
      executedQty:summary.netQty,
    });
    return {
      ok:false,
      status:"PROTECTION_NORMALIZATION_FAILED",
      reconciliationRequired:true,
      unprotectedPosition:true,
      mayResend:false,
      buy,
      intentId,
      reason:String(error?.message || error).slice(0,120),
    };
  }

  const oco = await placeOco(env, {
    intentId,
    signalId,
    symbol,
    quantity:normalized.quantity,
    takeProfit:normalized.takeProfit,
    stopLoss:normalized.stopLoss,
    stopLimit:normalized.stopLimit,
    eventSink,
  });

  if (oco.ok) {
    return {
      ok:true,
      status:"OCO_PLACED",
      intentId,
      partialFill:buy.partial === true,
      buy:{ body:{
        status:buy.partial ? "BUY_PARTIAL_FINAL" : "BUY_FILLED",
        order_id:buy.order?.orderId ?? null,
        executed_qty:summary.netQty,
        gross_executed_qty:summary.grossQty,
        quote_spent:summary.quoteSpent,
        weighted_price:summary.weightedPrice,
        commissions:summary.commissions,
      }},
      oco:{ body:{
        status:"OCO_PLACED",
        oco_order_list_id:oco.orderList?.orderListId ?? null,
      }},
      executedQty:summary.netQty,
      protectedQty:normalized.quantity,
      clientIds:oco.ids,
      filters:{
        stepSize:normalized.stepSize,
        tickSize:normalized.tickSize,
        minNotional:normalized.minNotional,
      },
      preTrade:preTrade.validation,
    };
  }

  if (oco.status === "OCO_SUBMISSION_UNKNOWN" || oco.status === "OCO_PRECHECK_UNKNOWN") {
    await writeTradeState(env, intentId, "PROTECTION_PENDING", {
      reason:oco.status,
      executedQty:summary.netQty,
      protectionUnknown:true,
    });
    return {
      ok:false,
      status:oco.status,
      reconciliationRequired:true,
      unprotectedPosition:true,
      mayResend:false,
      buy,
      oco,
      intentId,
      clientIds:oco.ids,
    };
  }

  const safeToClose = oco.definiteReject === true || oco.reconciliationConfirmedAbsent === true;
  if (!safeToClose) {
    await writeTradeState(env, intentId, "PROTECTION_PENDING", {
      reason:oco.status || "OCO_RECONCILIATION_REQUIRED",
      executedQty:summary.netQty,
    });
    return {
      ok:false,
      status:oco.status || "OCO_RECONCILIATION_REQUIRED",
      reconciliationRequired:true,
      unprotectedPosition:true,
      mayResend:false,
      buy,
      oco,
      intentId,
      clientIds:oco.ids,
    };
  }

  const closed = await emergencyClose(env, {
    intentId,
    signalId,
    symbol,
    quantity:normalized.quantity,
    eventSink,
  });
  if (!closed.ok) {
    await writeTradeState(env, intentId, "FAILED_SAFE", {
      reason:closed.status || "UNPROTECTED_POSITION",
      executedQty:summary.netQty,
      emergencyClosed:false,
    });
    return {
      ok:false,
      status:closed.status || "UNPROTECTED_POSITION",
      reconciliationRequired:true,
      unprotectedPosition:true,
      mayResend:false,
      buy,
      oco,
      emergency:closed,
      intentId,
      clientIds:oco.ids,
    };
  }

  return {
    ok:true,
    status:"PROTECTION_FAILED_EMERGENCY_CLOSED",
    intentId,
    buy:{ body:{
      status:buy.partial ? "BUY_PARTIAL_FINAL" : "BUY_FILLED",
      order_id:buy.order?.orderId ?? null,
      executed_qty:summary.netQty,
      gross_executed_qty:summary.grossQty,
      quote_spent:summary.quoteSpent,
      weighted_price:summary.weightedPrice,
      commissions:summary.commissions,
    }},
    oco:{ body:{
      status:"PROTECTION_FAILED_EMERGENCY_CLOSED",
      emergency_order_id:closed.emergencyOrder?.orderId ?? null,
    }},
    executedQty:summary.netQty,
    protectedQty:0,
    clientIds:oco.ids,
    preTrade:preTrade.validation,
  };
}
