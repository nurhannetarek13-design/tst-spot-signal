import test from "node:test";
import assert from "node:assert/strict";
import { applySide, sequenceStatus, bookMetrics, tradeMetrics } from "../ready_bot/stream_shadow.mjs";

test("depth sequence rejects gaps and ignores old events",()=>{
  assert.equal(sequenceStatus(100,{U:101,u:102}),"APPLY");
  assert.equal(sequenceStatus(100,{U:105,u:106}),"GAP");
  assert.equal(sequenceStatus(100,{U:90,u:100}),"OLD");
});

test("book flow tracks additions cancellations obi and microprice",()=>{
  const bids=new Map([[100,2],[99,1]]);
  const asks=new Map([[101,2],[102,1]]);
  const flow=[];
  applySide(bids,[["100","3"],["99","0.5"]],flow,1000,"bid");
  applySide(asks,[["101","1"],["102","2"]],flow,1000,"ask");
  const x=bookMetrics({bids,asks,flow},1000);
  assert.ok(Number.isFinite(x.obi));
  assert.ok(Number.isFinite(x.microprice));
  assert.ok(x.cancellationRate10s>0);
  assert.ok(x.bidCancelQuote10s>0);
});

test("agg trade metrics separate aggressive buys and sells",()=>{
  const trades=[
    {ts:1000,d:100},{ts:2000,d:200},{ts:3000,d:-50},
  ];
  const x=tradeMetrics(trades,4000);
  assert.equal(x.deltaQuote60s,250);
  assert.ok(x.takerBuyRatio60s>0.8);
});


test("book metrics expose V3 sidecar contract",()=>{
  const s={
    bids:new Map([[100,3],[99,2]]),
    asks:new Map([[101,2],[102,3]]),
    flow:[
      {ts:1000,side:"bid",type:"add",quote:100},
      {ts:1000,side:"ask",type:"cancel",quote:101},
    ],
  };
  const x=bookMetrics(s,1500);
  assert.ok(Number.isFinite(x.bidLiquidityQuote5));
  assert.ok(Number.isFinite(x.askLiquidityQuote5));
  assert.ok(Number.isFinite(x.obi));
  assert.ok(Number.isFinite(x.micropriceBiasBps));
  assert.ok(Number.isFinite(x.cancellationRate10s));
});

test("CVD slope requires improving and net-positive 10s buckets",()=>{
  const positive=tradeMetrics([
    {ts:1000,d:10},{ts:12000,d:20},{ts:22000,d:30},
  ],23000);
  assert.equal(positive.cvdSlopePositive10s,true);
  const negativeNet=tradeMetrics([
    {ts:1000,d:-100},{ts:12000,d:-20},{ts:22000,d:5},
  ],23000);
  assert.equal(negativeNet.cvdSlopePositive10s,false);
});
