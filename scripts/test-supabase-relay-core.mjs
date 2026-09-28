import assert from "node:assert/strict";
import { parseAndValidateCapability } from "../supabase/functions/tst-binance-relay/relay-core.mjs";

function q(obj = {}) {
  const p=new URLSearchParams();
  for(const [key,value] of Object.entries(obj)){
    if(value===undefined||value===null||value==="") continue;
    p.append(key,String(value));
  }
  p.sort();
  return p.toString();
}

const account=parseAndValidateCapability({
  method:"GET",path:"/api/v3/account",query:""
},{writesEnabled:false});
assert.equal(account.isWrite,false);
assert.equal(account.signatureType,"ED25519_SUPABASE_VAULT");

const tradingStatus=parseAndValidateCapability({
  method:"GET",path:"/sapi/v1/account/apiTradingStatus",query:""
},{writesEnabled:false});
assert.equal(tradingStatus.isWrite,false);

assert.throws(()=>parseAndValidateCapability({
  method:"GET",path:"/api/v3/account",query:"timestamp=1800000000000"
},{writesEnabled:false}),/CALLER_SECURITY_PARAM_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  method:"GET",path:"/api/v3/account",query:"signature=abc"
},{writesEnabled:false}),/CALLER_SECURITY_PARAM_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.50",newClientOrderId:"TSTB12345678"})
},{writesEnabled:false}),/FINANCIAL_WRITES_DISABLED/);

const buy=parseAndValidateCapability({
  method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.50",newClientOrderId:"TSTB12345678"})
},{writesEnabled:true});
assert.equal(buy.isWrite,true);

assert.throws(()=>parseAndValidateCapability({
  method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.51",newClientOrderId:"TSTB12345678"})
},{writesEnabled:true}),/QUOTE_CAP_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  method:"POST",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"LIMIT",quoteOrderQty:"5.50",newClientOrderId:"TSTB12345678"})
},{writesEnabled:true}),/ORDER_TYPE_BLOCKED/);

const oco=parseAndValidateCapability({
  method:"POST",path:"/api/v3/orderList/oco",
  query:q({
    symbol:"SOLUSDT",side:"SELL",quantity:"0.04",
    listClientOrderId:"TSTO12345678",
    aboveClientOrderId:"TSTT12345678",
    belowClientOrderId:"TSTS12345678",
    aboveType:"LIMIT_MAKER",abovePrice:"120",
    belowType:"STOP_LOSS_LIMIT",belowStopPrice:"118",belowPrice:"117.8"
  })
},{writesEnabled:true});
assert.equal(oco.isWrite,true);

assert.throws(()=>parseAndValidateCapability({
  method:"POST",path:"/api/v3/orderList/oco",
  query:q({
    symbol:"SOLUSDT",side:"SELL",quantity:"0.04",
    listClientOrderId:"BAD12345678",
    aboveClientOrderId:"TSTT12345678",
    belowClientOrderId:"TSTS12345678",
    aboveType:"LIMIT_MAKER",abovePrice:"120",
    belowType:"STOP_LOSS_LIMIT",belowStopPrice:"118",belowPrice:"117.8"
  })
},{writesEnabled:true}),/CLIENT_ID_PREFIX_BLOCKED/);

assert.throws(()=>parseAndValidateCapability({
  method:"GET",path:"/sapi/v1/capital/withdraw/apply",query:""
},{writesEnabled:false}),/PATH_BLOCKED/);

const dryRun=parseAndValidateCapability({
  method:"POST",path:"/api/v3/order/test",
  query:q({symbol:"SOLUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.50",newClientOrderId:"TSTBdryrun1234"})
},{writesEnabled:false});
assert.equal(dryRun.isWrite,false);
assert.equal(dryRun.isTest,true);

const duplicateQuery="symbol=SOLUSDT&symbol=BTCUSDT";
assert.throws(()=>parseAndValidateCapability({
  method:"GET",path:"/api/v3/order",query:duplicateQuery
},{writesEnabled:false}),/DUPLICATE_PARAM/);

assert.throws(()=>parseAndValidateCapability({
  method:"DELETE",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",origClientOrderId:"BADCLIENT123"})
},{writesEnabled:true}),/CLIENT_ID_PREFIX_BLOCKED/);

const cancel=parseAndValidateCapability({
  method:"DELETE",path:"/api/v3/order",
  query:q({symbol:"SOLUSDT",origClientOrderId:"TSTB12345678"})
},{writesEnabled:true});
assert.equal(cancel.isWrite,true);

console.log("SUPABASE_RELAY_CORE_SELFTEST_PASS");
console.log("SUPABASE_RELAY_DRYRUN_AND_DUPLICATE_GUARDS_PASS");
