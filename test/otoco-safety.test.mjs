import test from "node:test";
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
