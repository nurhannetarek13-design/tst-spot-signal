from pathlib import Path

SERVER = Path("server.mjs")
src = SERVER.read_text(encoding="utf-8")

MARKER = 'preferredEntryProtection: "OPOCO_WS_FOK_LIMIT_THEN_OTOCO_REST_THEN_MARKET_OCO"'
if MARKER in src:
    print("OPOCO executor patch already present")
    raise SystemExit(0)


def replace_once(old: str, new: str, label: str):
    global src
    count = src.count(old)
    if count != 1:
        raise SystemExit(f"Refusing patch: {label} expected once, found {count}")
    src = src.replace(old, new, 1)


replace_once(
    '    preferredEntryProtection: "OTOCO_FOK_LIMIT_THEN_MARKET_OCO",\n    otocoPrepared: true,',
    '    preferredEntryProtection: "OPOCO_WS_FOK_LIMIT_THEN_OTOCO_REST_THEN_MARKET_OCO",\n'
    '    opocoPrepared: true,\n'
    '    otocoPrepared: true,',
    "executor status",
)

# OPOCO working BUY and exit IDs use distinct prefixes so a zero-fill OPOCO
# attempt can safely fall back to OTOCO without client-order-id collisions.
if src.count('TST[BW]') != 2:
    raise SystemExit(f"Refusing patch: expected 2 BUY id matchers, found {src.count('TST[BW]')}")
src = src.replace('TST[BW]', 'TST[BWQ]')

if src.count('TST[TSX]') < 1:
    raise SystemExit("Refusing patch: no exit id matcher found")
src = src.replace('TST[TSX]', 'TST[TSUVX]')

entry_marker = '''  // Prefer Binance OTOCO when the symbol supports it. The working BUY is a
'''
if src.count(entry_marker) != 1:
    raise SystemExit("Refusing patch: OTOCO entry marker changed")

opoco_branch = r'''  // Prefer Binance OPOCO first. OPOCO is exposed by Binance Spot's signed
  // WebSocket API and automatically sizes the pending OCO from the quantity
  // actually received by the filled working BUY. A known zero-fill/rejection
  // can fall through to REST OTOCO; any ambiguous transport state fails closed.
  const opocoAttempt = await attemptOpocoFokEntry({
    symbol,
    token: signalHash,
    symbolInfo,
    openOrders,
    quoteToSpend,
    ask,
    plannedStop,
    plannedTakeProfit,
    stepSize,
    tickSize,
    minNotional,
    riskPct,
    openSymbols,
    cfg,
  });
  if (opocoAttempt.handled) return opocoAttempt.result;

'''
src = src.replace(entry_marker, opoco_branch + entry_marker, 1)

helper_marker = 'function canUseOtoco(symbolInfo, openOrders = []) {'
if src.count(helper_marker) != 1:
    raise SystemExit("Refusing patch: OTOCO helper marker changed")

helpers = r'''
function canUseOpoco(symbolInfo, openOrders = []) {
  // OPOCO is a special subset of OTOCO. Binance exchangeInfo exposes the OTO
  // and OCO capability flags, so both must be available and three order slots
  // must remain free before we even attempt the signed WebSocket request.
  return canUseOtoco(symbolInfo, openOrders);
}

function buildOpocoFokPlan(symbolInfo, { quoteToSpend, ask, stop, takeProfit, stepSize, tickSize, minNotional }) {
  const workingPrice = Number(ceilToStep(Number(ask) * 1.0005, Number(tickSize)));
  const workingQuantity = Number(floorToStep(Number(quoteToSpend) / workingPrice, Number(stepSize)));
  // OPOCO itself does NOT receive pendingQuantity. Binance uses the quantity
  // actually received from the working order. This conservative floor is only
  // a preflight estimate for LOT_SIZE/notional safety on a small account.
  const expectedReceivedFloor = Number(floorToStep(workingQuantity * (1 - CFG.feeRate), Number(stepSize)));
  const errors = [];
  const lot = (symbolInfo?.filters || []).find(f => f.filterType === "LOT_SIZE");
  const price = (symbolInfo?.filters || []).find(f => f.filterType === "PRICE_FILTER");

  if (!(workingPrice > 0 && workingQuantity > 0 && expectedReceivedFloor > 0)) {
    errors.push("invalid OPOCO price/quantity");
  }
  if (lot) {
    const min = Number(lot.minQty || 0);
    const max = Number(lot.maxQty || Number.MAX_VALUE);
    const step = Number(lot.stepSize || 0);
    if (!(workingQuantity >= min && workingQuantity <= max) || !isStepAligned(workingQuantity, min, step)) {
      errors.push("LOT_SIZE would reject OPOCO working quantity");
    }
    if (!(expectedReceivedFloor >= min && expectedReceivedFloor <= max)) {
      errors.push("LOT_SIZE safety floor is too small for OPOCO protection");
    }
  }
  if (price) {
    const min = Number(price.minPrice || 0);
    const max = Number(price.maxPrice || Number.MAX_VALUE);
    const tick = Number(price.tickSize || 0);
    for (const [label, value] of [["working", workingPrice], ["stop", Number(stop)], ["take-profit", Number(takeProfit)]]) {
      if (!(value >= min && value <= max) || !isStepAligned(value, min, tick)) {
        errors.push(`PRICE_FILTER would reject OPOCO ${label} price`);
      }
    }
  }
  if (workingQuantity * workingPrice < Number(minNotional)) errors.push("NOTIONAL would reject OPOCO working BUY");
  if (expectedReceivedFloor * Number(stop) < Number(minNotional)) errors.push("NOTIONAL would reject OPOCO stop leg");
  if (expectedReceivedFloor * Number(takeProfit) < Number(minNotional)) errors.push("NOTIONAL would reject OPOCO take-profit leg");
  if (!(Number(stop) > 0 && Number(stop) < Number(ask) && Number(takeProfit) > workingPrice)) {
    errors.push("OPOCO protection prices are not on the correct side of entry");
  }

  return { ok: errors.length === 0, errors, workingPrice, workingQuantity, expectedReceivedFloor };
}

function canonicalWsPayload(params) {
  return Object.entries(params || {})
    .filter(([, value]) => value !== undefined && value !== null)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${String(value)}`)
    .join("&");
}

async function wsDataToText(data) {
  if (typeof data === "string") return data;
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
  if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString("utf8");
  if (data && typeof data.arrayBuffer === "function") {
    return Buffer.from(await data.arrayBuffer()).toString("utf8");
  }
  return String(data);
}

async function signedBinanceWs(method, params = {}) {
  const apiKey = process.env.BINANCE_API_KEY;
  const privateKeyPem = process.env.BINANCE_API_PRIVATE_KEY;
  const secret = process.env.BINANCE_API_SECRET;
  if (!apiKey || (!privateKeyPem && !secret)) throw new Error("Binance API signing key is missing");
  if (typeof WebSocket !== "function") {
    const error = new Error("WebSocket API is unavailable in this runtime");
    error.executionDefinitelyRejected = true;
    throw error;
  }

  const unsigned = { ...params, apiKey, recvWindow: 5000, timestamp: Date.now() };
  const payload = canonicalWsPayload(unsigned);
  const signature = privateKeyPem
    ? await ed25519Base64(privateKeyPem, payload)
    : await hmacHex(secret, payload);
  const id = crypto.randomUUID();
  const request = { id, method, params: { ...unsigned, signature } };

  return await new Promise((resolve, reject) => {
    let settled = false;
    let opened = false;
    let requestSent = false;
    const ws = new WebSocket("wss://ws-api.binance.com:443/ws-api/v3");
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws.close(); } catch {}
      fn(value);
    };
    const timer = setTimeout(() => {
      const error = new Error("Binance WebSocket API timeout; execution status unknown");
      error.name = "BinanceWsTimeoutError";
      error.requestSent = requestSent;
      if (!requestSent) error.executionDefinitelyRejected = true;
      finish(reject, error);
    }, 12_000);

    ws.addEventListener("open", () => {
      opened = true;
      try {
        ws.send(JSON.stringify(request));
        requestSent = true;
      } catch (cause) {
        const error = new Error(`Binance WebSocket send failed: ${String(cause?.message || cause)}`);
        error.name = "BinanceWsTransportError";
        error.executionDefinitelyRejected = true;
        finish(reject, error);
      }
    });

    ws.addEventListener("message", async event => {
      try {
        const text = await wsDataToText(event.data);
        const message = JSON.parse(text);
        if (String(message.id) !== id) return;
        const status = Number(message.status || 0);
        if (status >= 200 && status < 300) {
          finish(resolve, message.result);
          return;
        }
        const error = new Error(`${message.error?.code || status || "WS"}: ${message.error?.msg || "Binance WebSocket request failed"}`);
        error.code = Number(message.error?.code || status || 0);
        error.httpStatus = status || null;
        error.binanceMessage = message.error?.msg || null;
        error.executionDefinitelyRejected = true;
        finish(reject, error);
      } catch (cause) {
        const error = new Error(`Binance WebSocket response parse failed: ${String(cause?.message || cause)}`);
        error.name = "BinanceWsProtocolError";
        error.requestSent = requestSent;
        finish(reject, error);
      }
    });

    ws.addEventListener("error", () => {
      const error = new Error(opened
        ? "Binance WebSocket transport error; execution status unknown"
        : "Binance WebSocket connection failed before submission");
      error.name = "BinanceWsTransportError";
      error.requestSent = requestSent;
      if (!requestSent) error.executionDefinitelyRejected = true;
      finish(reject, error);
    });

    ws.addEventListener("close", () => {
      if (settled) return;
      const error = new Error(requestSent
        ? "Binance WebSocket closed before a response; execution status unknown"
        : "Binance WebSocket closed before submission");
      error.name = "BinanceWsTransportError";
      error.requestSent = requestSent;
      if (!requestSent) error.executionDefinitelyRejected = true;
      finish(reject, error);
    });
  });
}

async function submitOpocoOnceAndReconcile(params) {
  const existing = await findOrderListByClientId(params.listClientOrderId);
  if (existing) return existing;
  try {
    return await signedBinanceWs("orderList.place.opoco", params);
  } catch (error) {
    if (isUnknownExecutionError(error) || error?.requestSent) {
      for (let attempt = 0; attempt < 5; attempt++) {
        const reconciled = await findOrderListByClientId(params.listClientOrderId).catch(() => null);
        if (reconciled) return reconciled;
        if (attempt < 4) await new Promise(resolve => setTimeout(resolve, 250 * (attempt + 1)));
      }
    }
    throw error;
  }
}

async function waitForOpocoProtection(symbol, token) {
  let latest = [];
  for (let attempt = 0; attempt < 8; attempt++) {
    latest = await signedBinance("GET", "/api/v3/allOrders", { symbol, limit: 1000 }).catch(() => []);
    const exits = (latest || []).filter(order =>
      order.side === "SELL" && new RegExp(`^TST[UV]${token}$`, "i").test(String(order.clientOrderId || ""))
    );
    const filled = exits.find(order => order.status === "FILLED");
    if (filled) return { active: false, closed: true, filled, orders: exits };
    const active = exits.filter(order => ["NEW", "PARTIALLY_FILLED", "PENDING_NEW"].includes(order.status));
    if (active.length >= 2) return { active: true, closed: false, orders: active };
    if (attempt < 7) await new Promise(resolve => setTimeout(resolve, 200 * (attempt + 1)));
  }
  return { active: false, closed: false, orders: [] };
}

async function netSellableBuyQuantity(symbol, symbolInfo, orderId, executedQty, stepSize) {
  const trades = await signedBinance("GET", "/api/v3/myTrades", { symbol, orderId, limit: 1000 }).catch(() => []);
  const baseCommission = (trades || [])
    .filter(f => f.commissionAsset === symbolInfo.baseAsset)
    .reduce((sum, f) => sum + Number(f.commission || 0), 0);
  return Number(floorToStep(Math.max(0, Number(executedQty) - baseCommission), stepSize));
}

async function attemptOpocoFokEntry({
  symbol, token, symbolInfo, openOrders, quoteToSpend, ask, plannedStop,
  plannedTakeProfit, stepSize, tickSize, minNotional, riskPct, openSymbols, cfg,
}) {
  if (!canUseOpoco(symbolInfo, openOrders)) {
    return { handled: false, fallbackReason: "OPOCO_NOT_AVAILABLE" };
  }

  const plan = buildOpocoFokPlan(symbolInfo, {
    quoteToSpend,
    ask,
    stop: plannedStop,
    takeProfit: plannedTakeProfit,
    stepSize,
    tickSize,
    minNotional,
  });
  if (!plan.ok) return { handled: false, fallbackReason: plan.errors.join("; ") };

  const listClientOrderId = `TSTC${token}`;
  const workingClientOrderId = `TSTQ${token}`;
  let list;
  try {
    list = await submitOpocoOnceAndReconcile({
      symbol,
      listClientOrderId,
      workingType: "LIMIT",
      workingSide: "BUY",
      workingClientOrderId,
      workingPrice: plan.workingPrice,
      workingQuantity: plan.workingQuantity,
      workingTimeInForce: "FOK",
      pendingSide: "SELL",
      pendingAboveType: "LIMIT_MAKER",
      pendingAboveClientOrderId: `TSTU${token}`,
      pendingAbovePrice: plannedTakeProfit,
      pendingBelowType: "STOP_LOSS",
      pendingBelowClientOrderId: `TSTV${token}`,
      pendingBelowStopPrice: plannedStop,
      newOrderRespType: "FULL",
    });
  } catch (error) {
    const reconciled = await findOrderListByClientId(listClientOrderId).catch(() => null);
    if (reconciled) {
      list = reconciled;
    } else if (error?.executionDefinitelyRejected === true) {
      // Binance explicitly rejected the OPOCO or the local runtime could not
      // submit it at all. There is no exposure, so REST OTOCO is a safe fallback.
      return { handled: false, fallbackReason: `OPOCO_SAFE_REJECT_${error?.code || error?.name || "UNKNOWN"}` };
    } else {
      return {
        handled: true,
        result: {
          ok: false,
          status: "OPOCO_EXECUTION_STATE_UNKNOWN",
          reason: String(error?.message || error),
          orderPlaced: null,
          entryMethod: "OPOCO_WS_FOK_LIMIT",
        },
      };
    }
  }

  const initialWorking = (list?.orderReports || []).find(o => o.clientOrderId === workingClientOrderId) ||
    await findOrderByClientId(symbol, workingClientOrderId).catch(() => null);
  const terminal = await waitForKnownOrderState(symbol, workingClientOrderId, initialWorking);
  if (!terminal.known) {
    return {
      handled: true,
      result: {
        ok: false,
        status: "OPOCO_EXECUTION_STATE_UNKNOWN",
        reason: "OPOCO working BUY state is unknown. OTOCO/market fallback is blocked to prevent a duplicate BUY.",
        orderPlaced: null,
        clientOrderId: workingClientOrderId,
        entryMethod: "OPOCO_WS_FOK_LIMIT",
      },
    };
  }

  const working = terminal.order;
  const executedQty = Number(working.executedQty || 0);
  if (working.status !== "FILLED") {
    if (executedQty > 0) {
      try { await cancelOrderListAndReconcile(symbol, listClientOrderId); } catch {}
      const partialQty = await netSellableBuyQuantity(symbol, symbolInfo, working.orderId, executedQty, stepSize);
      const closed = await emergencyClose(symbol, partialQty, token).catch(() => null);
      return {
        handled: true,
        result: {
          ok: Boolean(closed),
          status: closed ? "OPOCO_PARTIAL_SAFETY_CLOSED" : "CRITICAL_OPOCO_PARTIAL_EXPOSURE",
          reason: closed ? "Unexpected OPOCO partial fill was closed for safety." : "Unexpected OPOCO partial fill could not be closed.",
          orderPlaced: true,
          executedQty,
          entryMethod: "OPOCO_WS_FOK_LIMIT",
        },
      };
    }
    return { handled: false, fallbackReason: `OPOCO_FOK_${working.status || "NO_FILL"}` };
  }

  const quoteSpent = Number(working.cummulativeQuoteQty || (executedQty * plan.workingPrice));
  const avgFill = executedQty > 0 ? quoteSpent / executedQty : plan.workingPrice;
  const netSellQty = await netSellableBuyQuantity(symbol, symbolInfo, working.orderId, executedQty, stepSize);
  const protectionState = await waitForOpocoProtection(symbol, token);
  const actualProtectedQty = Number(protectionState.orders?.find(o => Number(o.origQty || 0) > 0)?.origQty || netSellQty);

  if (protectionState.active) {
    return {
      handled: true,
      result: {
        ok: true,
        status: "LIVE_SPOT_OPENED",
        symbol,
        orderId: working.orderId,
        quoteSpentUSDT: round(quoteSpent, 2),
        quantity: actualProtectedQty,
        avgFillPrice: fmt(avgFill),
        stop: plannedStop,
        takeProfit: plannedTakeProfit,
        protection: "OPOCO_OCO",
        entryMethod: "OPOCO_WS_FOK_LIMIT",
        orderListId: list?.orderListId ?? null,
        openPositionsAfter: openSymbols.includes(symbol) ? openSymbols.length : openSymbols.length + 1,
        maxOpenPositions: cfg.maxOpenPositions,
        riskUSDT: round(quoteSpent * riskPct + quoteSpent * CFG.feeRate * 2, 4),
      },
    };
  }

  if (protectionState.closed) {
    return {
      handled: true,
      result: {
        ok: true,
        status: "SIGNAL_ALREADY_CLOSED",
        symbol,
        orderId: working.orderId,
        exitOrderId: protectionState.filled?.orderId,
        quoteSpentUSDT: round(quoteSpent, 2),
        quantity: actualProtectedQty,
        avgFillPrice: fmt(avgFill),
        stop: plannedStop,
        takeProfit: plannedTakeProfit,
        protection: "OPOCO_OCO",
        entryMethod: "OPOCO_WS_FOK_LIMIT",
        riskUSDT: 0,
        openPositionsAfter: openSymbols.length,
        maxOpenPositions: cfg.maxOpenPositions,
      },
    };
  }

  try {
    await cancelOrderListAndReconcile(symbol, listClientOrderId);
  } catch (cancelError) {
    return {
      handled: true,
      result: {
        ok: false,
        status: "CRITICAL_OPOCO_PROTECTION_UNKNOWN",
        reason: `BUY filled, protection was not confirmed, and OPOCO cancellation could not be confirmed: ${cancelError.message}`,
        orderPlaced: true,
        orderId: working.orderId,
        entryMethod: "OPOCO_WS_FOK_LIMIT",
      },
    };
  }

  const account = await signedBinance("GET", "/api/v3/account", {}).catch(() => null);
  const freeBase = Number((account?.balances || []).find(b => b.asset === symbolInfo.baseAsset)?.free || 0);
  const closeQty = Number(floorToStep(Math.min(netSellQty, freeBase > 0 ? freeBase : netSellQty), stepSize));
  const closed = await emergencyClose(symbol, closeQty, token).catch(() => null);
  return {
    handled: true,
    result: {
      ok: Boolean(closed),
      status: closed ? "BOUGHT_THEN_SAFETY_CLOSED" : "UNPROTECTED_POSITION",
      reason: closed
        ? "OPOCO protection did not activate in time, so the filled position was closed immediately for safety."
        : "OPOCO protection did not activate and the emergency close could not be confirmed.",
      symbol,
      orderId: working.orderId,
      quoteSpentUSDT: round(quoteSpent, 2),
      quantity: closeQty,
      avgFillPrice: fmt(avgFill),
      stop: plannedStop,
      takeProfit: plannedTakeProfit,
      protection: closed ? "CLOSED_FAILSAFE" : "NONE",
      entryMethod: "OPOCO_WS_FOK_LIMIT",
      openPositionsAfter: openSymbols.length,
      maxOpenPositions: cfg.maxOpenPositions,
      riskUSDT: round(quoteSpent * riskPct + quoteSpent * CFG.feeRate * 2, 4),
    },
  };
}

'''
src = src.replace(helper_marker, helpers + helper_marker, 1)

old_export = 'export { isStepAligned, validateOrderFilters, isUnknownExecutionError, normalizeKnownSymbols, validateApprovedSignal, isTerminalOrder, canUseOtoco, buildOtocoFokPlan };'
new_export = 'export { isStepAligned, validateOrderFilters, isUnknownExecutionError, normalizeKnownSymbols, validateApprovedSignal, isTerminalOrder, canUseOpoco, buildOpocoFokPlan, canonicalWsPayload, canUseOtoco, buildOtocoFokPlan };'
replace_once(old_export, new_export, "server exports")

SERVER.write_text(src, encoding="utf-8")
print("Prepared OPOCO-first executor with OTOCO and market-OCO fallbacks; live trading flag unchanged")
