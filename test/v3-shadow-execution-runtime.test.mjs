import test from "node:test";
import assert from "node:assert/strict";
import {
  createV3ShadowReservationStore,
  executeV3ShadowIntent,
} from "../src/execution/v3-shadow-execution-runtime.mjs";

function intent(style="AGGRESSIVE_LIMIT"){
  return {
    schema:"V3_SPOT_EXECUTION_INTENT_V1",
    authorization:"SHADOW_ONLY",
    liveApproved:false,
    signalId:"sig123",
    symbol:"SOLUSDT",
    side:"BUY",
    quoteAmountUsdt:10,
    entry:{
      style,
      referencePrice:100,
      limitPrice:style==="AGGRESSIVE_LIMIT"?100.05:null,
      cancelAfterMs:10,
      maxCancelReplace:2,
    },
    protection:{required:true,stopPrice:98,takeProfitPrice:104},
    diagnostics:{},
  };
}

test("market shadow fill is protected and idempotent", async()=>{
  let buys=0,ocos=0;
  const exchange={
    async placeMarketBuy({clientOrderId}){
      buys++;
      return {clientOrderId,status:"FILLED",executedQty:0.1,cumulativeQuoteQty:10,averagePrice:100};
    },
    async getOrder({clientOrderId}){
      return {clientOrderId,status:"FILLED",executedQty:0.1,cumulativeQuoteQty:10,averagePrice:100};
    },
    async placeOcoSell(args){ocos++;return {orderListId:"oco-1",...args,status:"ACTIVE"};},
  };
  const reservations=createV3ShadowReservationStore();
  const first=await executeV3ShadowIntent({intent:intent("MARKET"),exchange,reservations,sleep:async()=>{}});
  assert.equal(first.ok,true);
  assert.equal(first.fill.executedQty,0.1);
  assert.equal(first.protection.status,"ACTIVE");
  assert.equal(buys,1);
  assert.equal(ocos,1);

  const second=await executeV3ShadowIntent({intent:intent("MARKET"),exchange,reservations,sleep:async()=>{}});
  assert.equal(second.duplicate,true);
  assert.equal(buys,1);
  assert.equal(ocos,1);
});

test("ambiguous placement reconciles by client order id", async()=>{
  let placeCalls=0,getCalls=0;
  const exchange={
    async placeMarketBuy(){placeCalls++;throw new Error("NETWORK_TIMEOUT_AFTER_SEND");},
    async getOrder({clientOrderId}){
      getCalls++;
      return {clientOrderId,status:"FILLED",executedQty:0.1,cumulativeQuoteQty:10,averagePrice:100};
    },
    async placeOcoSell(args){return {orderListId:"oco-1",...args,status:"ACTIVE"};},
  };
  const out=await executeV3ShadowIntent({
    intent:intent("MARKET"),exchange,
    reservations:createV3ShadowReservationStore(),
    sleep:async()=>{},
  });
  assert.equal(out.ok,true);
  assert.equal(placeCalls,1);
  assert.ok(getCalls>=1);
  assert.equal(out.fill.executedQty,0.1);
});

test("aggressive limit cancels and replaces an unfilled order", async()=>{
  const placed=[];
  const cancelled=[];
  const orderState=new Map();
  const exchange={
    async placeLimitBuy({clientOrderId,price,quoteAmountUsdt}){
      placed.push({clientOrderId,price,quoteAmountUsdt});
      const attempt=placed.length;
      const row=attempt===1
        ? {clientOrderId,status:"NEW",executedQty:0,cumulativeQuoteQty:0,price}
        : {clientOrderId,status:"FILLED",executedQty:0.1,cumulativeQuoteQty:10,averagePrice:100,price};
      orderState.set(clientOrderId,row);
      return row;
    },
    async getOrder({clientOrderId}){return orderState.get(clientOrderId);},
    async cancelOrder({clientOrderId}){
      cancelled.push(clientOrderId);
      const row={...orderState.get(clientOrderId),status:"CANCELED"};
      orderState.set(clientOrderId,row);
      return row;
    },
    async getBookTicker(){return {ask:100.02};},
    async placeOcoSell(args){return {orderListId:"oco-1",...args,status:"ACTIVE"};},
  };
  const out=await executeV3ShadowIntent({
    intent:intent(),exchange,
    reservations:createV3ShadowReservationStore(),
    sleep:async()=>{},
    pollMs:10,
  });
  assert.equal(out.ok,true);
  assert.equal(placed.length,2);
  assert.equal(cancelled.length,1);
  assert.equal(out.fill.executedQty,0.1);
  assert.equal(out.attempts.length,2);
});

test("partial fill stops chasing, cancels remainder, and protects filled quantity", async()=>{
  let placements=0,cancels=0,protectedQty=0;
  const state=new Map();
  const exchange={
    async placeLimitBuy({clientOrderId,price}){
      placements++;
      const row={clientOrderId,status:"PARTIALLY_FILLED",executedQty:0.04,cumulativeQuoteQty:4,averagePrice:100,price};
      state.set(clientOrderId,row);
      return row;
    },
    async getOrder({clientOrderId}){return state.get(clientOrderId);},
    async cancelOrder({clientOrderId}){
      cancels++;
      const row={...state.get(clientOrderId),status:"CANCELED"};
      state.set(clientOrderId,row);
      return row;
    },
    async getBookTicker(){return {ask:100.02};},
    async placeOcoSell(args){protectedQty=args.quantity;return {orderListId:"oco-p",...args,status:"ACTIVE"};},
  };
  const out=await executeV3ShadowIntent({
    intent:intent(),exchange,
    reservations:createV3ShadowReservationStore(),
    sleep:async()=>{},
    pollMs:10,
  });
  assert.equal(out.ok,true);
  assert.equal(placements,1);
  assert.equal(cancels,1);
  assert.equal(out.fill.executedQty,0.04);
  assert.equal(protectedQty,0.04);
});

test("no fill returns fail-closed without protective order", async()=>{
  let protection=0;
  const state=new Map();
  const exchange={
    async placeLimitBuy({clientOrderId,price}){
      const row={clientOrderId,status:"NEW",executedQty:0,cumulativeQuoteQty:0,price};
      state.set(clientOrderId,row);return row;
    },
    async getOrder({clientOrderId}){return state.get(clientOrderId);},
    async cancelOrder({clientOrderId}){
      const row={...state.get(clientOrderId),status:"CANCELED"};
      state.set(clientOrderId,row);return row;
    },
    async getBookTicker(){return {ask:101};},
    async placeOcoSell(){protection++;return {};},
  };
  const x=intent();
  x.entry.maxCancelReplace=0;
  const out=await executeV3ShadowIntent({
    intent:x,exchange,
    reservations:createV3ShadowReservationStore(),
    sleep:async()=>{},
    pollMs:10,
  });
  assert.equal(out.ok,false);
  assert.equal(out.reason,"NO_FILL");
  assert.equal(protection,0);
});

test("realized slippage breach is recorded, not hidden", async()=>{
  const exchange={
    async placeMarketBuy({clientOrderId}){
      return {clientOrderId,status:"FILLED",executedQty:0.099,cumulativeQuoteQty:10,averagePrice:101.010101};
    },
    async getOrder({clientOrderId}){
      return {clientOrderId,status:"FILLED",executedQty:0.099,cumulativeQuoteQty:10,averagePrice:101.010101};
    },
    async placeOcoSell(args){return {orderListId:"oco-1",...args,status:"ACTIVE"};},
  };
  const out=await executeV3ShadowIntent({
    intent:intent("MARKET"),exchange,
    reservations:createV3ShadowReservationStore(),
    sleep:async()=>{},
    maxRealizedSlippageBps:12,
  });
  assert.equal(out.ok,true);
  assert.equal(out.slippageBreach,true);
  assert.ok(out.realizedSlippageBps>12);
});
