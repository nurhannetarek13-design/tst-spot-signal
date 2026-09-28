import assert from "node:assert/strict";
import { parseAndValidateCapability } from "../supabase/functions/tst-binance-relay/relay-core.mjs";

const now=1_800_000_000_000;
const sig="a".repeat(64);
const base={apiKey:"A".repeat(32)};

function q(obj){
  const p=new URLSearchParams({...obj,recvWindow:"5000",timestamp:String(now),signature:sig});
  return p.toString();
}

const account=parseAndValidateCapability({
  ...base,method:"GET",path:"/api/v3/account",query:q({})
},{nowMs:now,writesEnabled:false});
assert.equal(account.isWrite,false);

assert.throws(()=>parseAndValidateCapability({
  ...base,method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.50",newClientOrderId:"TSTB12345678"})
},{nowMs:now,writesEnabled:false}),/FINANCIAL_WRITES_DISABLED/);

const buy=parseAndValidateCapability({
  ...base,method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.50",newClientOrderId:"TSTB12345678"})
},{nowMs:now,writesEnabled:true});
assert.equal(buy.isWrite,true);

assert.throws(()=>parseAndValidateCapability({
  ...base,method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.51",newClientOrderId:"TSTB12345678"})
},{nowMs:now,writesEnabled:true}),/QUOTE_CAP_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  ...base,method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"LIMIT",quoteOrderQty:"5.50",newClientOrderId:"TSTB12345678"})
},{nowMs:now,writesEnabled:true}),/ORDER_TYPE_BLOCKED/);

const oco=parseAndValidateCapability({
  ...base,method:"POST",path:"/api/v3/orderList/oco",
  query:q({
    symbol:"SOLUSDT",side:"SELL",quantity:"0.04",
    listClientOrderId:"TSTO12345678",
    aboveType:"LIMIT_MAKER",abovePrice:"120",
    belowType:"STOP_LOSS_LIMIT",belowStopPrice:"118",belowPrice:"117.8"
  })
},{nowMs:now,writesEnabled:true});
assert.equal(oco.isWrite,true);

assert.throws(()=>parseAndValidateCapability({
  ...base,method:"POST",path:"/api/v3/orderList/oco",
  query:q({
    symbol:"SOLUSDT",side:"SELL",quantity:"0.04",
    listClientOrderId:"BAD12345678",
    aboveType:"LIMIT_MAKER",abovePrice:"120",
    belowType:"STOP_LOSS_LIMIT",belowStopPrice:"118",belowPrice:"117.8"
  })
},{nowMs:now,writesEnabled:true}),/CLIENT_ID_PREFIX_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  ...base,method:"GET",path:"/sapi/v1/capital/withdraw/apply",query:q({})
},{nowMs:now,writesEnabled:false}),/PATH_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  ...base,method:"GET",path:"/api/v3/account",
  query:new URLSearchParams({recvWindow:"5000",timestamp:String(now-20000),signature:sig}).toString()
},{nowMs:now,writesEnabled:false}),/STALE_CAPABILITY/);

console.log("SUPABASE_RELAY_CORE_SELFTEST_PASS");
