import assert from "node:assert/strict";
import { normalizeSpotProtection, validateSpotMarketBuy, floorToFilterStep } from "../src/binance-spot-filters.js";

const originalFetch = globalThis.fetch;
globalThis.fetch = async (url) => ({
  ok: true,
  async json() {
    if (String(url).includes("/ticker/bookTicker")) {
      return { bidPrice: "83999.00", askPrice: "84000.00" };
    }
    return {
      symbols: [{
        symbol: "BTCUSDT",
        status: "TRADING",
        isSpotTradingAllowed: true,
        quoteAsset: "USDT",
        baseAsset: "BTC",
        orderTypes: ["LIMIT","MARKET"],
        quoteOrderQtyMarketAllowed: true,
        baseAssetPrecision: 8,
        quoteAssetPrecision: 8,
        filters: [
          { filterType: "PRICE_FILTER", tickSize: "0.01" },
          { filterType: "LOT_SIZE", minQty: "0.00001", maxQty: "1000", stepSize: "0.00001" },
          { filterType: "MARKET_LOT_SIZE", minQty: "0.00001", maxQty: "1000", stepSize: "0.00001" },
          { filterType: "NOTIONAL", minNotional: "5", applyMinToMarket: true }
        ]
      }]
    };
  }
});

const x = await normalizeSpotProtection("BTCUSDT", 0.00006549, 84750.987, 83850.123, 83682.422);
assert.equal(x.quantity, "0.00006");
assert.equal(x.takeProfit, "84750.98");
assert.equal(x.stopLoss, "83850.12");
assert.equal(x.stopLimit, "83682.42");
assert.equal(x.minNotional, 5);

await assert.rejects(
  () => normalizeSpotProtection("BTCUSDT", 0.00001, 84750, 83850, 83682),
  /PROTECTION_NOTIONAL_BELOW_MIN/
);

assert.equal(floorToFilterStep("1.23456789","0.001"),"1.234");
const buy = await validateSpotMarketBuy("BTCUSDT", 5.5, 20, { referencePrice: 84000 });
assert.equal(buy.ok, true);
assert.equal(buy.quoteOrderQty, "5.5");
assert.equal(buy.filters.minNotional, 5);
await assert.rejects(
  () => validateSpotMarketBuy("BTCUSDT", 4.99, 20, { referencePrice: 84000 }),
  /MARKET_NOTIONAL_BELOW_MIN/
);
await assert.rejects(
  () => validateSpotMarketBuy("BTCUSDT", 5.5, 2, { referencePrice: 84000 }),
  /INSUFFICIENT_QUOTE_BALANCE/
);

globalThis.fetch = originalFetch;
console.log("BINANCE_SPOT_FILTER_NORMALIZATION_PASS");
