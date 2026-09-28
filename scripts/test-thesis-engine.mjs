import assert from "node:assert/strict";
import {
  detectThesisSetup,
  higherTimeframeContext,
  calibrationBySetup,
  buildThesisDecision,
} from "../src/thesis-engine.js";

function bar(open,high,low,close,i,volume=100){
  return {open,high,low,close,openTime:i*900000,quoteVolume:volume};
}

const candles=[];
for(let i=0;i<38;i++){
  const drift=i*0.002;
  candles.push(bar(99.4+drift,100,99.0+drift,99.6+drift,i,100));
}
candles.push(bar(99.7,101.5,99.6,101.2,38,160)); // breakout
candles.push(bar(101.1,101.4,100.2,100.9,39,110)); // retest/hold
candles.push(bar(100.9,101.6,100.5,101.4,40,120));
candles.push(bar(101.3,101.8,100.8,101.6,41,125));

const setup=detectThesisSetup(candles);
assert.ok(setup, "structural setup should be detected without order-flow inputs");
assert.equal(setup.strategy,"BREAKOUT_RETEST_CONTINUATION");
assert.equal(setup.setup,"STRUCTURE_BREAK_RETEST_HOLD");

function trendBars(start=100){
  const out=[];
  for(let i=0;i<70;i++){
    const close=start+i*0.15;
    out.push(bar(close-0.1,close+0.2,close-0.2,close,i,100));
  }
  return out;
}
const htf=higherTimeframeContext(trendBars(),trendBars(),trendBars());
assert.equal(htf.bullishAlignment,true);

const lowSample=calibrationBySetup(
  Array.from({length:29},(_,i)=>({setup:"STRUCTURE_BREAK_RETEST_HOLD",pnl:i<18?0.1:-0.1}))
);
assert.equal(lowSample.STRUCTURE_BREAK_RETEST_HOLD.status,"EARLY_SAMPLE");

const lowDecision=buildThesisDecision({
  setup,
  candles,
  entry:101.6,
  htf,
  marketRegime:"TREND_OK",
  confirmations:{
    liquidity:true,
    flow:false,
    microstructure:true,
    executionQuality:true,
    relVolStrong:false,
    takerStrong:false,
    depthStrong:true,
    microStrong:true,
  },
  calibration:lowSample,
  minNetRR:1.8,
});
assert.equal(lowDecision.passed,false);
assert.equal(lowDecision.confirmations.required.flow,false);
assert.equal(lowDecision.calibration.calibratedWinProbability,null);

const calibrated=calibrationBySetup(
  Array.from({length:30},(_,i)=>({setup:"STRUCTURE_BREAK_RETEST_HOLD",pnl:i<21?0.12:-0.08}))
);
assert.equal(calibrated.STRUCTURE_BREAK_RETEST_HOLD.status,"CALIBRATED");

const readyDecision=buildThesisDecision({
  setup,
  candles,
  entry:101.6,
  htf,
  marketRegime:"TREND_OK",
  confirmations:{
    liquidity:true,
    flow:true,
    microstructure:true,
    executionQuality:true,
    relVolStrong:true,
    takerStrong:true,
    depthStrong:true,
    microStrong:true,
  },
  calibration:calibrated,
  minNetRR:1.0,
});
assert.equal(readyDecision.passed,true);
assert.ok(readyDecision.targets.tp1>101.6);
assert.ok(readyDecision.targets.tp2>readyDecision.targets.tp1);
assert.ok(readyDecision.calibration.calibratedWinProbability>0);
assert.ok(Array.isArray(readyDecision.premise) && readyDecision.premise.length>=3);

console.log("THESIS_ENGINE_SELFTEST_PASS");
