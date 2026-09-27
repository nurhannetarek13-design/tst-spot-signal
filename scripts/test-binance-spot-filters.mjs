import assert from "node:assert/strict";
import { normalizeSpotProtection } from "../src/binance-spot-filters.js";

const originalFetch = globalThis.fetch;
globalThis.fetch = async () => ({
  ok: true,
  async json() {
    return {
      symbols: [{
        symbol: "BTCUSDT",
        status: "TRADING",
        filters: [
          { filterType: "PRICE_FILTER", tickSize: "0.01" },
          { filterType: "LOT_SIZE", minQty: "0.00001", stepSize: "0.00001" },
          { filterType: "NOTIONAL", minNotional: "5" }
        ]
      }]
    };
  }
});

const x = await normalizeSpotProtection("BTCUSDT", 0.00006549, 84750.987, 83850.123, 83682.422);
assert.equal(x.quantity, 0.00006);
assert.equal(x.takeProfit, 84750.98);
assert.equal(x.stopLoss, 83850.12);
assert.equal(x.stopLimit, 83682.42);
assert.equal(x.minNotional, 5);

await assert.rejects(
  () => normalizeSpotProtection("BTCUSDT", 0.00001, 84750, 83850, 83682),
  /PROTECTION_NOTIONAL_BELOW_MIN/
);

globalThis.fetch = originalFetch;
console.log("BINANCE_SPOT_FILTER_NORMALIZATION_PASS");
