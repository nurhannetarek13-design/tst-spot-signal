#!/usr/bin/env node
// Research-only historical evidence recovery + challenger manifest.
// Never imports execution modules and never authorizes live trading.
import fs from "node:fs/promises";
const read=async p=>JSON.parse(await fs.readFile(p,"utf8"));
const b365=await read("validation/backtest-365d.json");
const sr=await read("validation/strategy-research-365d.json");
const fixed=await read("validation/indicator-only-fixed-confirmation-latest.json");
const clean=await read("validation/round2-clean-holdout-result.json");
const evidence={
 generatedAt:new Date().toISOString(),engine:"HISTORICAL_MIX_CHALLENGER_V1",authorization:"RESEARCH_ONLY",liveTrading:false,
 productionMutation:false,financialAction:false,
 currentProductionStrategies:["STOP_HUNT_STRUCTURE_SHIFT","LIQUIDITY_SWEEP_FVG","BREAKOUT_RETEST_CONTINUATION","COMPRESSION_EXPANSION"],
 recovered:{
  legacy365:{quality:"APPROXIMATE_NOT_CURRENT_ENGINE",trades:b365.all.trades,oosTrades:b365.outOfSample.trades,pf:b365.all.profitFactor,oosPf:b365.outOfSample.profitFactor,netReturnPct:b365.all.netReturnPct,historicalOrderBook:b365.assumptions.historicalOrderBook},
  legacyFamilies:sr.results.map(x=>({strategy:x.strategy,trades:x.trades,pf:x.profitFactor,oosPf:x.outOfSampleProfitFactor,approved:x.approved})),
  fixedIndicator:{quality:"NON_CURRENT_ENGINE_DIAGNOSTIC",trades:fixed.aggregate.trades,winRate:fixed.aggregate.winRate,netPnlPerUnit:fixed.aggregate.netPnlPerUnit,note:"High win rate did not imply positive expectancy."},
  cleanHoldout:{quality:"UNTOUCHED_SYMBOL_HOLDOUT_LEGACY_FAMILIES",verdict:clean.verdict,reports:clean.reports.map(x=>({id:x.id,trades:x.onePosition.base.trades,pf:x.onePosition.base.profitFactor,expectancy:x.onePosition.base.expectancy}))}
 },
 rejectedForPromotion:[
  "Gate A V1 forward definition: closed before first trade due live/historical candle timing mismatch.",
  "Gate A V2 historical diagnostic PF ~0.468: not a positive production candidate.",
  "Legacy breakout/continuation/momentum/mean-reversion families: failed OOS gates in archived evidence.",
  "Old L2 confirmation: accepted 0/26 baseline entries and showed no incremental edge."
 ],
 challengerRules:{
  objective:"Improve portfolio expectancy after costs without increasing live risk.",
  evaluateBy:["strategy","regime","strategy×regime","score bucket"],
  require:["chronological OOS","walk-forward stability","positive expectancy after costs","PF >= 1.20","stress-cost positive expectancy","no lookahead","point-in-time universe where available"],
  promotion:"FORWARD_SHADOW_ONLY_AFTER_HISTORICAL_PASS",
  production:"UNCHANGED_UNTIL_NEW_UNSEEN_EVIDENCE"
 }
};
await fs.mkdir("validation/research",{recursive:true});
await fs.writeFile("validation/research/historical-mix-challenger-v1.json",JSON.stringify(evidence,null,2)+"\n");
console.log(JSON.stringify(evidence,null,2));
