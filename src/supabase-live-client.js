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

async function relay(env, method, path, params = {}, timeoutMs = 15000) {
  const url = String(env.SUPABASE_BINANCE_RELAY_URL || "").trim();
  const pair = binanceCredentials(env);
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

  const auth = await buildRelayAuth(pair, method, path, signed.query);
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
    const diagnostics = safeSigningDiagnostics({
      ...diagnosticBase,
      httpStatus: r.status,
      binanceCode: body?.binanceCode ?? null,
    });
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
