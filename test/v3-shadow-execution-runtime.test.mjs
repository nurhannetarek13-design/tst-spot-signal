import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  createV3FileReservationStore,
  createV3ShadowReservationStore,
  executeV3ShadowIntent,
  recoverV3ShadowReservations,
} from "../src/execution/v3-shadow-execution-runtime.mjs";

function intent(style="AGGRESSIVE_LIMIT"){
  return {
    schema:"V3_SPOT_EXECUTION_INTENT_V1",
    authorization:"SHADOW_ONLY",
    liveApproved:false,
    signalId:"sig123",
    createdAtMs:Date.now(),
    decisionLatencyMs:1000,
    maxTotalLatencyMs:8000,
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


test("file-backed reservations survive restart and stay idempotent", async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),"v3-res-"));
  const file=path.join(dir,"reservations.json");
  const a=await createV3FileReservationStore(file);
  const key="v3:SOLUSDT:persisted";
  const first=await a.reserve(key,{intent:intent("MARKET"),clientOrderIds:["v3-sig123-0"]});
  assert.equal(first.ok,true);

  const b=await createV3FileReservationStore(file);
  const second=await b.reserve(key,{intent:intent("MARKET")});
  assert.equal(second.ok,false);
  const pending=await b.listPending();
  assert.equal(pending.length,1);
  assert.equal(pending[0].key,key);
});

test("crash recovery reconciles known order and protects filled quantity", async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),"v3-recover-"));
  const file=path.join(dir,"reservations.json");
  const reservations=await createV3FileReservationStore(file);
  const x=intent("MARKET");
  const key="v3:SOLUSDT:"+x.signalId;
  await reservations.reserve(key,{
    intent:x,
    clientOrderIds:["v3-sig123-0"],
    stage:"PLACING_ENTRY",
  });

  let oco=0;
  const exchange={
    async getOrder({clientOrderId}){
      return {clientOrderId,status:"FILLED",executedQty:0.1,cumulativeQuoteQty:10,averagePrice:100};
    },
    async placeOcoSell(args){oco++;return {orderListId:"recovery-oco",...args,status:"ACTIVE"};},
  };
  const out=await recoverV3ShadowReservations({exchange,reservations});
  assert.equal(out.ok,true);
  assert.equal(out.pendingCount,1);
  assert.equal(out.recovered[0].status,"RECOVERED");
  assert.equal(out.recovered[0].executedQty,0.1);
  assert.equal(oco,1);

  const restarted=await createV3FileReservationStore(file);
  assert.equal((await restarted.listPending()).length,0);
  const saved=await restarted.get(key);
  assert.equal(saved.status,"COMPLETED");
  assert.equal(saved.result,"RECOVERED_FILLED_AND_PROTECTED");
});

test("crash recovery cancels open remainder before protecting partial fill", async()=>{
  const reservations=createV3ShadowReservationStore();
  const x=intent("AGGRESSIVE_LIMIT");
  const key="v3:SOLUSDT:"+x.signalId;
  await reservations.reserve(key,{
    intent:x,
    clientOrderIds:["v3-sig123-0"],
    stage:"PLACING_ENTRY",
  });

  let cancelled=0,protected=0;
  const exchange={
    async getOrder({clientOrderId}){
      return {clientOrderId,status:"PARTIALLY_FILLED",executedQty:0.04,cumulativeQuoteQty:4,averagePrice:100};
    },
    async cancelOrder({clientOrderId}){
      cancelled++;
      return {clientOrderId,status:"CANCELED",executedQty:0.04,cumulativeQuoteQty:4,averagePrice:100};
    },
    async placeOcoSell(args){protected=args.quantity;return {orderListId:"partial-recovery",...args,status:"ACTIVE"};},
  };
  const out=await recoverV3ShadowReservations({exchange,reservations});
  assert.equal(out.ok,true);
  assert.equal(cancelled,1);
  assert.equal(protected,0.04);
});


test("stale intent is rejected before any exchange order", async()=>{
  let calls=0;
  const x=intent("MARKET");
  x.createdAtMs=10_000;
  x.decisionLatencyMs=6_000;
  x.maxTotalLatencyMs=8_000;
  const exchange={
    async placeMarketBuy(){calls++;return {};},
  };
  await assert.rejects(
    ()=>executeV3ShadowIntent({
      intent:x,
      exchange,
      reservations:createV3ShadowReservationStore(),
      now:()=>13_000,
    }),
    /STALE_EXECUTION_INTENT/
  );
  assert.equal(calls,0);
});
