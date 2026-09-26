import test from "node:test";
import assert from "node:assert/strict";
import {
  v3PaperPositionToExecutionIntent,
  executionIntentIdempotencyKey,
  assertV3IntentShadowOnly,
} from "../src/execution/v3-execution-intent.mjs";

function position(style="AGGRESSIVE_LIMIT"){
  return {
    engine:"INDICATOR_ONLY_V3_EARLY_MOMENTUM",
    signal_id:"abc123",
    entry:100,
    stop:98,
    target:104,
    cost:5,
    momentum_score:91,
    market_regime:"TREND",
    opened_at:new Date().toISOString(),
    entry_context:{decision_latency_ms:1200,max_total_latency_ms:8000},
    execution_plan:{
      style,
      reference_price:100,
      limit_price:style==="AGGRESSIVE_LIMIT"?100.05:null,
      cancel_after_ms:1500,
      max_cancel_replace:2,
      estimated_slippage_bps:3,
    },
  };
}

test("V3 intent preserves smart execution and protection",()=>{
  const x=v3PaperPositionToExecutionIntent("SOLUSDT",position());
  assert.equal(x.authorization,"SHADOW_ONLY");
  assert.equal(x.decisionLatencyMs,1200);
  assert.equal(x.maxTotalLatencyMs,8000);
  assert.ok(Number.isFinite(x.createdAtMs));
  assert.equal(x.liveApproved,false);
  assert.equal(x.entry.style,"AGGRESSIVE_LIMIT");
  assert.equal(x.entry.limitPrice,100.05);
  assert.equal(x.protection.stopPrice,98);
  assert.equal(x.protection.takeProfitPrice,104);
});

test("V3 idempotency key is deterministic from signal id",()=>{
  const x=v3PaperPositionToExecutionIntent("SOLUSDT",position("MARKET"));
  assert.equal(executionIntentIdempotencyKey(x),"v3:SOLUSDT:abc123");
  assert.equal(assertV3IntentShadowOnly(x),true);
});

test("V3 live intent cannot be manufactured silently",()=>{
  const x=v3PaperPositionToExecutionIntent("SOLUSDT",position());
  x.liveApproved=true;
  assert.throws(()=>assertV3IntentShadowOnly(x),/V3_LIVE_INTENT_FORBIDDEN/);
});
