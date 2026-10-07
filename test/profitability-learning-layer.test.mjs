import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
const src=fs.readFileSync(new URL("../src/edge-worker.js",import.meta.url),"utf8");
test("profitability learning stays observe-only",()=>{assert.match(src,/OBSERVE_ONLY_NO_LIVE_GATING/);assert.match(src,/affectsLiveDecision:false/);assert.match(src,/changesLiveSizing:false/);assert.match(src,/changesLiveEligibility:false/);});
test("learning captures attribution and path quality",()=>{for(const token of ["learning:decision-log","byStrategyRegime","byScoreBucket","avgMfePct","avgMaePct","feesUSDT","metrics:best.metrics"])assert.ok(src.includes(token),token);});
test("learning layer does not enable autonomous execution",()=>{assert.ok(!src.includes("changesLiveSizing:true"));assert.ok(!src.includes("changesLiveEligibility:true"));});
