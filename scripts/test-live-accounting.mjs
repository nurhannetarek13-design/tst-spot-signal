import assert from "node:assert/strict";
import { summarizeBinanceOrder, compareAccounting, roundTripAccounting } from "../src/live-accounting.js";

const buy=summarizeBinanceOrder({
  symbol:"BTCUSDT",orderId:123,status:"FILLED",executedQty:"0.00006000",cummulativeQuoteQty:"5.04000000",
  fills:[
    {commissionAsset:"BTC",commission:"0.00000006"},
    {commissionAsset:"BNB",commission:"0.000001"}
  ]
},{commissionQuoteRates:{BNB:600}});
assert.equal(buy.grossQty,0.00006);
assert.equal(buy.netBaseQty,0.00005994);
assert.equal(buy.weightedPrice,84000);
assert.equal(buy.convertedOtherCommissionUSDT,0.0006);

const sell=summarizeBinanceOrder({
  symbol:"BTCUSDT",orderId:124,status:"FILLED",executedQty:"0.00005994",cummulativeQuoteQty:"5.09490000",
  fills:[{commissionAsset:"USDT",commission:"0.0050949"}]
});
const rt=roundTripAccounting({buy,sell,residualQty:0,residualMarkPrice:0});
assert.equal(Number(rt.grossPnlUSDT.toFixed(8)),0.0549);
assert.equal(Number(rt.feeUSDT.toFixed(7)),0.0056949);

const a={...buy,residualQty:0,grossPnlUSDT:rt.grossPnlUSDT,netPnlUSDT:rt.netPnlUSDT};
const b={...buy,residualQty:0,grossPnlUSDT:rt.grossPnlUSDT,netPnlUSDT:rt.netPnlUSDT};
assert.equal(compareAccounting(a,b).clean,true);
assert.equal(compareAccounting({...a,quoteQty:a.quoteQty+0.01},b).clean,false);

console.log("LIVE_ACCOUNTING_PARITY_SELFTEST_PASS");
