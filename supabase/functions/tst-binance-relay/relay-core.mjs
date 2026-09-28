export const READ_PATHS = new Set([
  "/api/v3/account",
  "/api/v3/openOrders",
  "/api/v3/order",
  "/api/v3/orderList",
  "/api/v3/openOrderList",
  "/api/v3/allOrderList",
  "/api/v3/allOrders",
  "/api/v3/myTrades",
  "/sapi/v1/account/apiRestrictions",
  "/sapi/v1/account/status",
]);

export const TEST_PATHS = new Set([
  "/api/v3/order/test",
]);

export const WRITE_PATHS = new Set([
  "/api/v3/order",
  "/api/v3/orderList/oco",
  "/api/v3/orderList",
]);

const SAFE_CLIENT_PREFIXES = ["TSTB","TSTX","TSTC","TSTQ","TSTO","TSTU","TSTV","TSTW","TSTS","TSTT"];

export function parseAndValidateCapability(body, { writesEnabled = false } = {}) {
  const method = String(body?.method || "").toUpperCase();
  const path = String(body?.path || "");
  const query = String(body?.query || "");
  writesEnabled = writesEnabled === true;

  if (!["GET","POST","DELETE"].includes(method)) throw new Error("METHOD_BLOCKED");
  if (!(READ_PATHS.has(path) || TEST_PATHS.has(path) || WRITE_PATHS.has(path))) throw new Error("PATH_BLOCKED");
  if (query.length > 4096) throw new Error("BAD_QUERY");

  const q = new URLSearchParams(query);
  const keys=[...q.keys()];
  const duplicateKeys=[...new Set(keys.filter((key,index)=>keys.indexOf(key)!==index))];
  if (duplicateKeys.length) throw new Error("DUPLICATE_PARAM");
  if (q.has("signature") || q.has("timestamp") || q.has("recvWindow")) {
    throw new Error("CALLER_SECURITY_PARAM_BLOCKED");
  }
  for (const key of keys) {
    if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key)) throw new Error("BAD_PARAM_NAME");
    const value=String(q.get(key) ?? "");
    if (value.length > 512) throw new Error("BAD_PARAM_VALUE");
  }

  const isTest = TEST_PATHS.has(path);
  const isWrite = method !== "GET" && !isTest;
  if (isWrite && !writesEnabled) throw new Error("FINANCIAL_WRITES_DISABLED");
  if (isWrite || isTest) validateWrite(path, method, q);

  return {
    method,
    path,
    query,
    q,
    isWrite,
    isTest,
    unsignedQuery:query,
    signatureType:"ED25519_SUPABASE_VAULT",
  };
}

function requireSpotSymbol(q) {
  const symbol = String(q.get("symbol") || "");
  if (!/^[A-Z0-9]{2,20}USDT$/.test(symbol)) throw new Error("BAD_SPOT_SYMBOL");
  return symbol;
}

function requireClientId(value, allowed = SAFE_CLIENT_PREFIXES) {
  const id = String(value || "");
  if (id.length < 8 || id.length > 36 || !/^[A-Za-z0-9_-]+$/.test(id)) throw new Error("BAD_CLIENT_ID");
  if (!allowed.some(prefix => id.startsWith(prefix))) throw new Error("CLIENT_ID_PREFIX_BLOCKED");
  return id;
}

function finitePositive(v, name) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) throw new Error(name);
  return n;
}

function validateWrite(path, method, q) {
  if (path === "/api/v3/order" && method === "DELETE") {
    requireSpotSymbol(q);
    requireClientId(q.get("origClientOrderId"), ["TSTB","TSTQ"]);
    return;
  }

  if ((path === "/api/v3/order" || path === "/api/v3/order/test") && method === "POST") {
    requireSpotSymbol(q);
    const side = String(q.get("side") || "").toUpperCase();
    const type = String(q.get("type") || "").toUpperCase();
    if (!["BUY","SELL"].includes(side)) throw new Error("SIDE_BLOCKED");
    if (type !== "MARKET") throw new Error("ORDER_TYPE_BLOCKED");
    requireClientId(q.get("newClientOrderId"), side === "BUY" ? ["TSTB","TSTQ"] : ["TSTX"]);

    if (side === "BUY") {
      const quote = finitePositive(q.get("quoteOrderQty"), "BAD_QUOTE");
      if (quote < 5 || quote > 5.5) throw new Error("QUOTE_CAP_BLOCKED");
      if (q.get("quantity")) throw new Error("BUY_QUANTITY_BLOCKED");
    } else {
      finitePositive(q.get("quantity"), "BAD_SELL_QTY");
      if (q.get("quoteOrderQty")) throw new Error("SELL_QUOTE_BLOCKED");
    }
    return;
  }

  if (path === "/api/v3/orderList/oco" && method === "POST") {
    requireSpotSymbol(q);
    if (String(q.get("side") || "").toUpperCase() !== "SELL") throw new Error("OCO_SIDE_BLOCKED");
    finitePositive(q.get("quantity"), "BAD_OCO_QTY");
    requireClientId(q.get("listClientOrderId"), ["TSTO","TSTC"]);
    requireClientId(q.get("aboveClientOrderId"), ["TSTT","TSTU"]);
    requireClientId(q.get("belowClientOrderId"), ["TSTS","TSTV"]);
    const aboveType = String(q.get("aboveType") || "").toUpperCase();
    const belowType = String(q.get("belowType") || "").toUpperCase();
    if (aboveType !== "LIMIT_MAKER") throw new Error("OCO_ABOVE_TYPE_BLOCKED");
    if (belowType !== "STOP_LOSS_LIMIT") throw new Error("OCO_BELOW_TYPE_BLOCKED");
    const tp = finitePositive(q.get("abovePrice"), "BAD_TP");
    const stop = finitePositive(q.get("belowStopPrice"), "BAD_STOP");
    const stopLimit = finitePositive(q.get("belowPrice"), "BAD_STOP_LIMIT");
    if (!(tp > stop && stop > stopLimit)) throw new Error("OCO_GEOMETRY_BLOCKED");
    return;
  }

  if (path === "/api/v3/orderList" && method === "DELETE") {
    requireSpotSymbol(q);
    const listId = q.get("listClientOrderId");
    if (!listId) throw new Error("LIST_CLIENT_ID_REQUIRED");
    requireClientId(listId, ["TSTO","TSTC"]);
    return;
  }

  throw new Error("WRITE_ROUTE_BLOCKED");
}

export function sanitizeResponse(path, method, data) {
  if (path === "/api/v3/account" && method === "GET") {
    return {
      canTrade: Boolean(data?.canTrade),
      accountType: data?.accountType || null,
      balances: Array.isArray(data?.balances)
        ? data.balances
            .filter(x => Number(x?.free || 0) > 0 || Number(x?.locked || 0) > 0)
            .map(x => ({ asset: x.asset, free: x.free, locked: x.locked }))
        : [],
    };
  }
  return data;
}
