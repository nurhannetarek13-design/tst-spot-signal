import test from "node:test";
import assert from "node:assert/strict";
import { canUseOpoco, buildOpocoFokPlan, canonicalWsPayload } from "../server.mjs";

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

test("OPOCO is only selected when the symbol advertises OTO and OCO support", () => {
  assert.equal(canUseOpoco(symbolInfo, []), true);
  assert.equal(canUseOpoco({ ...symbolInfo, otoAllowed: false }, []), false);
  assert.equal(canUseOpoco({ ...symbolInfo, ocoAllowed: false }, []), false);
});

test("OPOCO reserves three open-order slots", () => {
  const twoOpen = [{ symbol: "TESTUSDT" }, { symbol: "TESTUSDT" }];
  const threeOpen = [...twoOpen, { symbol: "TESTUSDT" }];
  assert.equal(canUseOpoco(symbolInfo, twoOpen), true);
  assert.equal(canUseOpoco(symbolInfo, threeOpen), false);
});

test("OPOCO plan validates a conservative received quantity without sending pendingQuantity", () => {
  const plan = buildOpocoFokPlan(symbolInfo, {
    quoteToSpend: 7,
    ask: 10,
    stop: 9.8,
    takeProfit: 10.5,
    stepSize: 0.001,
    tickSize: 0.01,
    minNotional: 5,
  });
  assert.equal(plan.ok, true, plan.errors.join("; "));
  assert.ok(plan.workingQuantity > 0);
  assert.ok(plan.expectedReceivedFloor > 0);
  assert.ok(plan.expectedReceivedFloor <= plan.workingQuantity);
  assert.equal("pendingQuantity" in plan, false);
  assert.ok(plan.expectedReceivedFloor * 9.8 >= 5);
});

test("OPOCO plan fails closed when the likely received quantity cannot protect the stop leg", () => {
  const plan = buildOpocoFokPlan(symbolInfo, {
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

test("WebSocket signing payload is deterministic and alphabetically sorted", () => {
  const payload = canonicalWsPayload({
    timestamp: 123,
    symbol: "BTCUSDT",
    apiKey: "abc",
    workingQuantity: "0.001",
    recvWindow: 5000,
  });
  assert.equal(payload, "apiKey=abc&recvWindow=5000&symbol=BTCUSDT&timestamp=123&workingQuantity=0.001");
});
