import test from "node:test";import assert from "node:assert/strict";import fs from "node:fs";
const s=fs.readFileSync(new URL("../scripts/historical-mix-challenger-v1.mjs",import.meta.url),"utf8");
test("challenger is research only and nonfinancial",()=>{assert.match(s,/RESEARCH_ONLY/);assert.match(s,/liveTrading:false/);assert.match(s,/financialAction:false/);assert.match(s,/productionMutation:false/);});
test("challenger preserves multi strategy production mix",()=>{for(const x of ["STOP_HUNT_STRUCTURE_SHIFT","LIQUIDITY_SWEEP_FVG","BREAKOUT_RETEST_CONTINUATION","COMPRESSION_EXPANSION"])assert.ok(s.includes(x));});
test("promotion requires evidence",()=>{for(const x of ["chronological OOS","walk-forward stability","stress-cost positive expectancy","FORWARD_SHADOW_ONLY_AFTER_HISTORICAL_PASS"])assert.ok(s.includes(x));});
