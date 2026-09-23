import test from "node:test";
import assert from "node:assert/strict";
import { validateSignedOperation, policyConstants } from "../lib/binance-signed-passthrough-policy.mjs";

const now=1_790_192_400_000;
const sig="a".repeat(64);
function q(params) {
  const x=new URLSearchParams();
  for (const [k,v] of Object.entries(params)) x.append(k,String(v));
  x.append("recvWindow","5000");
  x.append("timestamp",String(now));
  x.append("signature",sig);
  return x.toString();
}

test("allows account read only with exact signed shape",()=>{
  assert.equal(validateSignedOperation("GET","/api/v3/account",q({}),now).kind,"ACCOUNT_READ");
  assert.equal(validateSignedOperation("GET","/api/v3/account",q({foo:"1"}),now).ok,false);
});

test("allows bounded spot market buy",()=>{
  const row=validateSignedOperation("POST","/api/v3/order",q({
    symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"7.00",
    newOrderRespType:"FULL",newClientOrderId:"TSTUabc12345",
  }),now);
  assert.equal(row.ok,true);
  assert.equal(row.kind,"SPOT_MARKET_BUY");
  const tooLarge=validateSignedOperation("POST","/api/v3/order",q({
    symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:String(policyConstants.MAX_BUY_QUOTE_USDT+0.01),
    newOrderRespType:"FULL",newClientOrderId:"TSTUabc12345",
  }),now);
  assert.equal(tooLarge.status,"BUY_QUOTE_OUTSIDE_LIMIT");
});

test("allows only deterministic emergency market sell",()=>{
  const ok=validateSignedOperation("POST","/api/v3/order",q({
    symbol:"SOLUSDT",side:"SELL",type:"MARKET",quantity:"0.1",
    newOrderRespType:"FULL",newClientOrderId:"TSTEabc12345",
  }),now);
  assert.equal(ok.kind,"SPOT_EMERGENCY_MARKET_SELL");
  const bad=validateSignedOperation("POST","/api/v3/order",q({
    symbol:"SOLUSDT",side:"SELL",type:"LIMIT",quantity:"0.1",
    newOrderRespType:"FULL",newClientOrderId:"TSTEabc12345",
  }),now);
  assert.equal(bad.ok,false);
});

test("allows valid sell OCO and rejects bad geometry",()=>{
  const base={
    symbol:"SOLUSDT",side:"SELL",quantity:"0.1",
    aboveType:"LIMIT_MAKER",abovePrice:"105",
    belowType:"STOP_LOSS_LIMIT",belowStopPrice:"99",belowPrice:"98.5",
    belowTimeInForce:"GTC",listClientOrderId:"TSTOabc12345",
  };
  assert.equal(validateSignedOperation("POST","/api/v3/orderList/oco",q(base),now).kind,"SPOT_OCO_SELL");
  assert.equal(validateSignedOperation("POST","/api/v3/orderList/oco",q({...base,abovePrice:"97"}),now).status,"OCO_GEOMETRY_INVALID");
});

test("rejects futures, margin, withdraw and duplicate keys",()=>{
  for (const path of ["/fapi/v1/order","/sapi/v1/margin/order","/sapi/v1/capital/withdraw/apply"]) {
    assert.equal(validateSignedOperation("POST",path,q({symbol:"SOLUSDT"}),now).status,"OPERATION_NOT_ALLOWED");
  }
  const dup=q({})+"&timestamp="+now;
  assert.equal(validateSignedOperation("GET","/api/v3/account",dup,now).status,"DUPLICATE_QUERY_KEY");
});

test("rejects stale query",()=>{
  assert.equal(validateSignedOperation("GET","/api/v3/account",q({}),now+61_000).status,"STALE_BINANCE_QUERY");
});
