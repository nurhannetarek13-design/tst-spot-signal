from pathlib import Path

SERVER = Path("server.mjs")
TEST = Path("test/otoco-safety.test.mjs")
src = SERVER.read_text(encoding="utf-8")

MARKER = 'preferredEntryProtection: "OTOCO_FOK_LIMIT_THEN_MARKET_OCO"'
if MARKER in src:
    print("OTOCO executor patch already present")
    raise SystemExit(0)


def replace_once(old: str, new: str, label: str):
    global src
    count = src.count(old)
    if count != 1:
        raise SystemExit(f"Refusing patch: {label} expected once, found {count}")
    src = src.replace(old, new, 1)


replace_once(
    '    strategyRelease: VALIDATED_STRATEGY_RELEASE,\n    tradeUSDT: cfg.tradeUSDT,',
    '    strategyRelease: VALIDATED_STRATEGY_RELEASE,\n'
    '    preferredEntryProtection: "OTOCO_FOK_LIMIT_THEN_MARKET_OCO",\n'
    '    otocoPrepared: true,\n'
    '    tradeUSDT: cfg.tradeUSDT,',
    "executor status marker",
)

replace_once(
    '/^TSTB[a-f0-9]{8}$/i.test(String(o.clientOrderId || ""))',
    '/^TST[BW][a-f0-9]{8}$/i.test(String(o.clientOrderId || ""))',
    "recovery entry id matcher",
)

replace_once(
    'id.match(/^TSTB([a-f0-9]{8})$/i);',
    'id.match(/^TST[BW]([a-f0-9]{8})$/i);',
    "PnL entry id matcher",
)

injection_point = '''  // Deterministic IDs make Binance the final idempotency barrier. Replaying the
  // same approved signal must reconcile the first order, never create a new BUY.
'''
if src.count(injection_point) != 1:
    raise SystemExit("Refusing patch: deterministic BUY marker changed")

otoco_branch = r'''  // Prefer Binance OTOCO when the symbol supports it. The working BUY is a
  // marketable FOK LIMIT: it either fills completely and Binance activates the
  // pending OCO atomically, or it expires with zero fill and we safely fall
  // back to the existing MARKET -> confirmed FILLED -> OCO path below.
  const otocoAttempt = await attemptOtocoFokEntry({
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
  if (otocoAttempt.handled) return otocoAttempt.result;

'''
src = src.replace(injection_point, otoco_branch + injection_point, 1)

helper_marker = 'function isSafeLiveSpotSymbol(symbol) {'
if src.count(helper_marker) != 1:
    raise SystemExit("Refusing patch: live symbol helper marker changed")

helpers = r'''
function canUseOtoco(symbolInfo, openOrders = []) {
  if (!symbolInfo || symbolInfo.otoAllowed !== true || symbolInfo.ocoAllowed !== true) return false;
  const symbol = String(symbolInfo.symbol || "");
  const currentForSymbol = (openOrders || []).filter(o => o.symbol === symbol).length;
  const maxOrdersFilter = (symbolInfo.filters || []).find(f => f.filterType === "MAX_NUM_ORDERS");
  const maxOrders = Number(maxOrdersFilter?.maxNumOrders || 0);
  if (maxOrders > 0 && currentForSymbol + 3 > maxOrders) return false;
  return true;
}

function buildOtocoFokPlan(symbolInfo, { quoteToSpend, ask, stop, takeProfit, stepSize, tickSize, minNotional }) {
  const workingPrice = Number(ceilToStep(Number(ask) * 1.0005, Number(tickSize)));
  const workingQuantity = Number(floorToStep(Number(quoteToSpend) / workingPrice, Number(stepSize)));
  // Keep a fee buffer so a base-asset commission cannot make the pending SELL
  // quantity larger than the amount actually available after the BUY fill.
  const pendingQuantity = Number(floorToStep(workingQuantity * (1 - CFG.feeRate), Number(stepSize)));
  const errors = [];
  const lot = (symbolInfo?.filters || []).find(f => f.filterType === "LOT_SIZE");
  const price = (symbolInfo?.filters || []).find(f => f.filterType === "PRICE_FILTER");

  if (!(workingPrice > 0 && workingQuantity > 0 && pendingQuantity > 0)) errors.push("invalid OTOCO price/quantity");
  if (lot) {
    const min = Number(lot.minQty || 0);
    const max = Number(lot.maxQty || Number.MAX_VALUE);
    const step = Number(lot.stepSize || 0);
    if (!(workingQuantity >= min && workingQuantity <= max) || !isStepAligned(workingQuantity, min, step)) {
      errors.push("LOT_SIZE would reject OTOCO working quantity");
    }
    if (!(pendingQuantity >= min && pendingQuantity <= max) || !isStepAligned(pendingQuantity, min, step)) {
      errors.push("LOT_SIZE would reject OTOCO pending quantity");
    }
  }
  if (price) {
    const min = Number(price.minPrice || 0);
    const max = Number(price.maxPrice || Number.MAX_VALUE);
    const tick = Number(price.tickSize || 0);
    for (const [label, value] of [["working", workingPrice], ["stop", Number(stop)], ["take-profit", Number(takeProfit)]]) {
      if (!(value >= min && value <= max) || !isStepAligned(value, min, tick)) errors.push(`PRICE_FILTER would reject OTOCO ${label} price`);
    }
  }
  if (workingQuantity * workingPrice < Number(minNotional)) errors.push("NOTIONAL would reject OTOCO working BUY");
  if (pendingQuantity * Number(stop) < Number(minNotional)) errors.push("NOTIONAL would reject OTOCO stop leg");
  if (pendingQuantity * Number(takeProfit) < Number(minNotional)) errors.push("NOTIONAL would reject OTOCO take-profit leg");
  if (!(Number(stop) > 0 && Number(stop) < Number(ask) && Number(takeProfit) > workingPrice)) {
    errors.push("OTOCO protection prices are not on the correct side of entry");
  }

  return {
    ok: errors.length === 0,
    errors,
    workingPrice,
    workingQuantity,
    pendingQuantity,
  };
}

async function waitForOtocoProtection(symbol, token) {
  let latest = [];
  for (let attempt = 0; attempt < 8; attempt++) {
    latest = await signedBinance("GET", "/api/v3/allOrders", { symbol, limit: 1000 }).catch(() => []);
    const exits = (latest || []).filter(order =>
      order.side === "SELL" && new RegExp(`^TST[TS]${token}$`, "i").test(String(order.clientOrderId || ""))
    );
    const filled = exits.find(order => order.status === "FILLED");
    if (filled) return { active: false, closed: true, filled, orders: exits };
    const active = exits.filter(order => ["NEW", "PARTIALLY_FILLED", "PENDING_NEW"].includes(order.status));
    if (active.length >= 2) return { active: true, closed: false, orders: active };
    if (attempt < 7) await new Promise(resolve => setTimeout(resolve, 200 * (attempt + 1)));
  }
  return { active: false, closed: false, orders: [] };
}

async function submitOtocoOnceAndReconcile(params) {
  const existing = await findOrderListByClientId(params.listClientOrderId);
  if (existing) return existing;
  try {
    return await signedBinance("POST", "/api/v3/orderList/otoco", params);
  } catch (error) {
    if (error?.code === -2010 || isUnknownExecutionError(error)) {
      for (let attempt = 0; attempt < 5; attempt++) {
        const reconciled = await findOrderListByClientId(params.listClientOrderId).catch(() => null);
        if (reconciled) return reconciled;
        if (attempt < 4) await new Promise(resolve => setTimeout(resolve, 250 * (attempt + 1)));
      }
    }
    throw error;
  }
}

async function cancelOrderListAndReconcile(symbol, listClientOrderId) {
  try {
    return await signedBinance("DELETE", "/api/v3/orderList", { symbol, listClientOrderId });
  } catch (error) {
    if (isUnknownExecutionError(error) || error?.code === -2011 || error?.code === -2022) {
      for (let attempt = 0; attempt < 5; attempt++) {
        const list = await findOrderListByClientId(listClientOrderId).catch(() => null);
        if (list && ["ALL_DONE", "REJECT"].includes(String(list.listStatusType || ""))) return list;
        if (attempt < 4) await new Promise(resolve => setTimeout(resolve, 250 * (attempt + 1)));
      }
    }
    throw error;
  }
}

async function attemptOtocoFokEntry({
  symbol, token, symbolInfo, openOrders, quoteToSpend, ask, plannedStop,
  plannedTakeProfit, stepSize, tickSize, minNotional, riskPct, openSymbols, cfg,
}) {
  if (!canUseOtoco(symbolInfo, openOrders)) {
    return { handled: false, fallbackReason: "OTOCO_NOT_AVAILABLE" };
  }

  const plan = buildOtocoFokPlan(symbolInfo, {
    quoteToSpend,
    ask,
    stop: plannedStop,
    takeProfit: plannedTakeProfit,
    stepSize,
    tickSize,
    minNotional,
  });
  if (!plan.ok) return { handled: false, fallbackReason: plan.errors.join("; ") };

  const listClientOrderId = `TSTO${token}`;
  const workingClientOrderId = `TSTW${token}`;
  let list;
  try {
    list = await submitOtocoOnceAndReconcile({
      symbol,
      listClientOrderId,
      workingType: "LIMIT",
      workingSide: "BUY",
      workingClientOrderId,
      workingPrice: plan.workingPrice,
      workingQuantity: plan.workingQuantity,
      workingTimeInForce: "FOK",
      pendingSide: "SELL",
      pendingQuantity: plan.pendingQuantity,
      pendingAboveType: "LIMIT_MAKER",
      pendingAboveClientOrderId: `TSTT${token}`,
      pendingAbovePrice: plannedTakeProfit,
      pendingBelowType: "STOP_LOSS",
      pendingBelowClientOrderId: `TSTS${token}`,
      pendingBelowStopPrice: plannedStop,
      newOrderRespType: "FULL",
    });
  } catch (error) {
    // Never fall back after an ambiguous OTOCO submission: a second BUY could
    // duplicate exposure. Reconciliation on the next run is safer.
    const reconciled = await findOrderListByClientId(listClientOrderId).catch(() => null);
    if (!reconciled) {
      return {
        handled: true,
        result: {
          ok: false,
          status: isUnknownExecutionError(error) ? "OTOCO_EXECUTION_STATE_UNKNOWN" : "OTOCO_REJECTED",
          reason: String(error?.message || error),
          orderPlaced: isUnknownExecutionError(error) ? null : false,
          entryMethod: "OTOCO_FOK_LIMIT",
        },
      };
    }
    list = reconciled;
  }

  const initialWorking = (list?.orderReports || []).find(o => o.clientOrderId === workingClientOrderId) ||
    await findOrderByClientId(symbol, workingClientOrderId).catch(() => null);
  const terminal = await waitForKnownOrderState(symbol, workingClientOrderId, initialWorking);
  if (!terminal.known) {
    return {
      handled: true,
      result: {
        ok: false,
        status: "OTOCO_EXECUTION_STATE_UNKNOWN",
        reason: "OTOCO working BUY state is unknown. Market fallback is blocked to prevent a duplicate BUY.",
        orderPlaced: null,
        clientOrderId: workingClientOrderId,
        entryMethod: "OTOCO_FOK_LIMIT",
      },
    };
  }

  const working = terminal.order;
  const executedQty = Number(working.executedQty || 0);
  if (working.status !== "FILLED") {
    if (executedQty > 0) {
      // FOK should never partially fill. If Binance reports exposure anyway,
      // cancel the list and close only the confirmed quantity; do not fallback.
      try { await cancelOrderListAndReconcile(symbol, listClientOrderId); } catch {}
      const partialQty = Number(floorToStep(executedQty * (1 - CFG.feeRate), stepSize));
      const closed = await emergencyClose(symbol, partialQty, token).catch(() => null);
      return {
        handled: true,
        result: {
          ok: Boolean(closed),
          status: closed ? "OTOCO_PARTIAL_SAFETY_CLOSED" : "CRITICAL_OTOCO_PARTIAL_EXPOSURE",
          reason: closed ? "Unexpected OTOCO partial fill was closed for safety." : "Unexpected OTOCO partial fill could not be closed.",
          orderPlaced: true,
          executedQty,
          entryMethod: "OTOCO_FOK_LIMIT",
        },
      };
    }
    // FOK expired/canceled/rejected with zero fill: there is no position, so
    // the original MARKET -> FILLED -> OCO path is a safe fallback.
    return { handled: false, fallbackReason: `OTOCO_FOK_${working.status || "NO_FILL"}` };
  }

  const quoteSpent = Number(working.cummulativeQuoteQty || (executedQty * plan.workingPrice));
  const avgFill = executedQty > 0 ? quoteSpent / executedQty : plan.workingPrice;
  const baseAsset = symbolInfo.baseAsset;
  const buyTrades = await signedBinance("GET", "/api/v3/myTrades", {
    symbol,
    orderId: working.orderId,
    limit: 1000,
  }).catch(() => []);
  const baseCommission = (buyTrades || [])
    .filter(f => f.commissionAsset === baseAsset)
    .reduce((sum, f) => sum + Number(f.commission || 0), 0);
  const netSellQty = Number(floorToStep(Math.max(0, executedQty - baseCommission), stepSize));

  const protectionState = await waitForOtocoProtection(symbol, token);
  if (protectionState.active) {
    return {
      handled: true,
      result: {
        ok: true,
        status: "LIVE_SPOT_OPENED",
        symbol,
        orderId: working.orderId,
        quoteSpentUSDT: round(quoteSpent, 2),
        quantity: plan.pendingQuantity,
        avgFillPrice: fmt(avgFill),
        stop: plannedStop,
        takeProfit: plannedTakeProfit,
        protection: "OCO",
        entryMethod: "OTOCO_FOK_LIMIT",
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
        quantity: plan.pendingQuantity,
        avgFillPrice: fmt(avgFill),
        stop: plannedStop,
        takeProfit: plannedTakeProfit,
        protection: "OCO",
        entryMethod: "OTOCO_FOK_LIMIT",
        riskUSDT: 0,
        openPositionsAfter: openSymbols.length,
        maxOpenPositions: cfg.maxOpenPositions,
      },
    };
  }

  // A filled working BUY without two confirmed pending protection legs is not
  // allowed to remain open. Cancel the list first so a delayed OCO cannot race
  // an emergency SELL, then close the confirmed net quantity.
  try {
    await cancelOrderListAndReconcile(symbol, listClientOrderId);
  } catch (cancelError) {
    return {
      handled: true,
      result: {
        ok: false,
        status: "CRITICAL_OTOCO_PROTECTION_UNKNOWN",
        reason: `BUY filled, protection was not confirmed, and OTOCO cancellation could not be confirmed: ${cancelError.message}`,
        orderPlaced: true,
        orderId: working.orderId,
        entryMethod: "OTOCO_FOK_LIMIT",
      },
    };
  }

  const closed = await emergencyClose(symbol, netSellQty, token).catch(() => null);
  return {
    handled: true,
    result: {
      ok: Boolean(closed),
      status: closed ? "BOUGHT_THEN_SAFETY_CLOSED" : "UNPROTECTED_POSITION",
      reason: closed
        ? "OTOCO protection did not activate in time, so the filled position was closed immediately for safety."
        : "OTOCO protection did not activate and the emergency close could not be confirmed.",
      symbol,
      orderId: working.orderId,
      quoteSpentUSDT: round(quoteSpent, 2),
      quantity: netSellQty,
      avgFillPrice: fmt(avgFill),
      stop: plannedStop,
      takeProfit: plannedTakeProfit,
      protection: closed ? "CLOSED_FAILSAFE" : "NONE",
      entryMethod: "OTOCO_FOK_LIMIT",
      openPositionsAfter: openSymbols.length,
      maxOpenPositions: cfg.maxOpenPositions,
      riskUSDT: round(quoteSpent * riskPct + quoteSpent * CFG.feeRate * 2, 4),
    },
  };
}

'''
src = src.replace(helper_marker, helpers + helper_marker, 1)

replace_once(
    'export { isStepAligned, validateOrderFilters, isUnknownExecutionError, normalizeKnownSymbols, validateApprovedSignal, isTerminalOrder };',
    'export { isStepAligned, validateOrderFilters, isUnknownExecutionError, normalizeKnownSymbols, validateApprovedSignal, isTerminalOrder, canUseOtoco, buildOtocoFokPlan };',
    "server exports",
)

SERVER.write_text(src, encoding="utf-8")

TEST.write_text(r'''import test from "node:test";
import assert from "node:assert/strict";
import { canUseOtoco, buildOtocoFokPlan } from "../server.mjs";

const symbolInfo = {
  symbol: "TESTUSDT",
  otoAllowed: true,
  ocoAllowed: true,
  filters: [
    { filterType: "LOT_SIZE", minQty: "0.001", maxQty: "1000", stepSize: "0.001" },
    { filterType: "PRICE_FILTER", minPrice: "0.01", maxPrice: "100000", tickSize: "0.01" },
    { filterType: "NOTIONAL", minNotional: "5", maxNotional: "100000" },
    { filterType: "MAX_NUM_ORDERS", maxNumOrders: 5 },
  ],
};

test("OTOCO is only selected when Binance advertises OTO and OCO support", () => {
  assert.equal(canUseOtoco(symbolInfo, []), true);
  assert.equal(canUseOtoco({ ...symbolInfo, otoAllowed: false }, []), false);
  assert.equal(canUseOtoco({ ...symbolInfo, ocoAllowed: false }, []), false);
});

test("OTOCO reserves three order slots before entry", () => {
  const twoOpen = [{ symbol: "TESTUSDT" }, { symbol: "TESTUSDT" }];
  const threeOpen = [...twoOpen, { symbol: "TESTUSDT" }];
  assert.equal(canUseOtoco(symbolInfo, twoOpen), true);
  assert.equal(canUseOtoco(symbolInfo, threeOpen), false);
});

test("OTOCO FOK plan is step-aligned and keeps both protection legs above notional", () => {
  const plan = buildOtocoFokPlan(symbolInfo, {
    quoteToSpend: 7,
    ask: 10,
    stop: 9.8,
    takeProfit: 10.5,
    stepSize: 0.001,
    tickSize: 0.01,
    minNotional: 5,
  });
  assert.equal(plan.ok, true, plan.errors.join("; "));
  assert.ok(plan.workingPrice >= 10);
  assert.ok(plan.pendingQuantity <= plan.workingQuantity);
  assert.ok(plan.pendingQuantity * 9.8 >= 5);
  assert.ok(plan.pendingQuantity * 10.5 >= 5);
});

test("OTOCO plan fails closed when small capital cannot protect both legs", () => {
  const plan = buildOtocoFokPlan(symbolInfo, {
    quoteToSpend: 5,
    ask: 10,
    stop: 9.8,
    takeProfit: 10.5,
    stepSize: 0.001,
    tickSize: 0.01,
    minNotional: 5,
  });
  assert.equal(plan.ok, false);
  assert.ok(plan.errors.some(x => x.includes("stop leg")));
});
''', encoding="utf-8")

print("Patched server.mjs with OTOCO FOK-first execution and safety tests")
