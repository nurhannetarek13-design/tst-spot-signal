import test from "node:test";
import assert from "node:assert/strict";
import { isStepAligned, validateOrderFilters, isUnknownExecutionError } from "../server.mjs";

const symbolInfo = {
  filters: [
    { filterType: "MARKET_LOT_SIZE", minQty: "0.001", maxQty: "100", stepSize: "0.001" },
    { filterType: "LOT_SIZE", minQty: "0.001", maxQty: "100", stepSize: "0.001" },
    { filterType: "PRICE_FILTER", minPrice: "0.01", maxPrice: "100000", tickSize: "0.01" },
    { filterType: "NOTIONAL", minNotional: "5", maxNotional: "100000" },
  ],
};

test("step alignment handles exchange increments", () => {
  assert.equal(isStepAligned(1.234, 0.001, 0.001), true);
  assert.equal(isStepAligned(1.2345, 0.001, 0.001), false);
});

test("quoteOrderQty buy does not require estimated quantity step alignment", () => {
  const errors = validateOrderFilters(symbolInfo, {
    marketQuantity: 0.030251,
    usesQuoteOrderQty: true,
    quoteOrderQty: 5.2,
    sellQuantity: 0.03,
    stopPrice: 170,
    takeProfitPrice: 180,
  });
  assert.deepEqual(errors, []);
});

test("protection fails closed below Binance notional", () => {
  const errors = validateOrderFilters(symbolInfo, {
    marketQuantity: 0.03,
    usesQuoteOrderQty: true,
    quoteOrderQty: 5.2,
    sellQuantity: 0.029,
    stopPrice: 170,
    takeProfitPrice: 180,
  });
  assert.ok(errors.includes("NOTIONAL would reject the stop leg"));
});

test("network ambiguity is classified for reconciliation", () => {
  assert.equal(isUnknownExecutionError({ code: -1007 }), true);
  assert.equal(isUnknownExecutionError(new Error("request timed out")), true);
  assert.equal(isUnknownExecutionError({ code: -1013 }), false);
});
