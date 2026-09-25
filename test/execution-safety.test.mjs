import test from "node:test";
import assert from "node:assert/strict";
import { isStepAligned, validateOrderFilters, isUnknownExecutionError, normalizeKnownSymbols, validateApprovedSignal, isTerminalOrder, evaluateApiRestrictions, evaluateAccountTradingSafety } from "../server.mjs";
import { validateOperation } from "../api/binance-signed-relay.mjs";
import { evaluateCloudflareAccountSafety } from "../src/buy-gateway-stable.js";

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

test("executor rejects unsigned or malformed strategy decisions", () => {
  assert.equal(validateApprovedSignal(null, "BTCUSDT", "12345678").ok, false);
  assert.equal(validateApprovedSignal({ symbol: "BTCUSDT", valid: true, score: 95, plan: { entry: 100, stop: 101, target1: 110 } }, "BTCUSDT", "12345678").ok, false);
  assert.equal(validateApprovedSignal({ symbol: "BTCUSDT", valid: true, score: 95, plan: { entry: 100, stop: 99, target1: 110 }, approvedAt: Date.now() }, "BTCUSDT", "12345678").ok, true);
  assert.equal(validateApprovedSignal({ symbol: "BTCUSDT", valid: true, score: 95, plan: { entry: 100, stop: 99, target1: 110 }, approvedAt: Date.now() - 600_000 }, "BTCUSDT", "12345678").ok, false);
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

test("daily-loss universe includes every validated traded symbol", () => {
  const symbols = normalizeKnownSymbols(["pendleusdt", "SKHYBUSDT", "BAD/USDT", "BTCUSDT"], "BERAUSDT");
  assert.ok(symbols.includes("PENDLEUSDT"));
  assert.ok(symbols.includes("SKHYBUSDT"));
  assert.ok(symbols.includes("BERAUSDT"));
  assert.equal(symbols.includes("BAD/USDT"), false);
});

test("partial fills are exposure, never a terminal successful buy", () => {
  assert.equal(isTerminalOrder({ status: "PARTIALLY_FILLED", executedQty: "0.1" }), false);
  assert.equal(isTerminalOrder({ status: "FILLED", executedQty: "0.1" }), true);
});


test("API key safety rejects withdrawals futures margin and missing IP restriction", () => {
  const safe=evaluateApiRestrictions({
    ipRestrict:true,enableReading:true,enableWithdrawals:false,
    enableMargin:false,enableFutures:false,enableSpotAndMarginTrading:true,
  });
  assert.equal(safe.ok,true);
  const bad=evaluateApiRestrictions({
    ipRestrict:false,enableReading:true,enableWithdrawals:true,
    enableMargin:true,enableFutures:true,
  });
  assert.equal(bad.ok,false);
  assert.ok(bad.reasons.includes("WITHDRAWALS_MUST_BE_DISABLED"));
  assert.ok(bad.reasons.includes("FUTURES_MUST_BE_DISABLED"));
  assert.ok(bad.reasons.includes("IP_RESTRICTION_REQUIRED"));
});


test("Account trading safety blocks locked or non-normal accounts", () => {
  const safe=evaluateAccountTradingSafety({data:"Normal"},{data:{isLocked:false,plannedRecoverTime:0}});
  assert.equal(safe.ok,true);

  const locked=evaluateAccountTradingSafety({data:"Normal"},{data:{isLocked:true,plannedRecoverTime:123}});
  assert.equal(locked.ok,false);
  assert.ok(locked.reasons.includes("API_TRADING_LOCKED"));

  const abnormal=evaluateAccountTradingSafety({data:"Maintenance"},{data:{isLocked:false,plannedRecoverTime:0}});
  assert.equal(abnormal.ok,false);
  assert.ok(abnormal.reasons.includes("ACCOUNT_STATUS_NOT_NORMAL"));
});


test("signed relay allows only the read-only account safety GETs", () => {
  for (const path of [
    "/sapi/v1/account/apiRestrictions",
    "/sapi/v1/account/status",
    "/sapi/v1/account/apiTradingStatus",
  ]) {
    assert.equal(validateOperation("GET",path,{}).ok,true);
    assert.equal(validateOperation("POST",path,{}).ok,false);
    assert.equal(validateOperation("GET",path,{foo:"bar"}).ok,false);
  }
  assert.equal(validateOperation("GET","/sapi/v1/capital/config/getall",{}).ok,false);
});

test("cloudflare live safety requires spot-only permissions and normal unlocked account", () => {
  const safe=evaluateCloudflareAccountSafety(
    {
      enableReading:true,enableSpotAndMarginTrading:true,
      enableWithdrawals:false,enableFutures:false,enableMargin:false,ipRestrict:true,
    },
    {data:"Normal"},
    {data:{isLocked:false,plannedRecoverTime:0}},
  );
  assert.equal(safe.ok,true);

  const bad=evaluateCloudflareAccountSafety(
    {
      enableReading:true,enableSpotAndMarginTrading:true,
      enableWithdrawals:true,enableFutures:true,enableMargin:true,ipRestrict:false,
    },
    {data:"Maintenance"},
    {data:{isLocked:true,plannedRecoverTime:123}},
  );
  assert.equal(bad.ok,false);
  for (const reason of [
    "WITHDRAWALS_MUST_BE_DISABLED","FUTURES_MUST_BE_DISABLED",
    "MARGIN_MUST_BE_DISABLED","IP_RESTRICTION_REQUIRED",
    "ACCOUNT_STATUS_NOT_NORMAL","API_TRADING_LOCKED",
  ]) assert.ok(bad.reasons.includes(reason));
});
