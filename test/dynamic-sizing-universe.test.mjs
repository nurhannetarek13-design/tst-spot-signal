import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const gateway = await readFile(new URL("../src/buy-gateway-stable.js", import.meta.url), "utf8");
const worker = await readFile(new URL("../src/edge-worker.js", import.meta.url), "utf8");
const policy = await readFile(new URL("../src/live-cutover-policy.js", import.meta.url), "utf8");
const relay = await readFile(new URL("../supabase/functions/tst-binance-relay/relay-core.mjs", import.meta.url), "utf8");
const wrangler = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");

test("manual live sizing is dynamic, risk-capped, balance-capped, and not fixed at 5.5", () => {
  assert.match(gateway, /DEFAULT_MAX_ORDER_USDT = 25/);
  assert.match(gateway, /MAX_BALANCE_FRACTION = 0\.50/);
  assert.match(gateway, /MAX_RISK_USDT = 0\.20/);
  assert.match(gateway, /qualityRiskBudgetUSDT\(score\)/);
  assert.match(gateway, /dynamicQuote\(/);
  assert.match(gateway, /refreshBalance\(env\)/);
  assert.match(gateway, /DYNAMIC_SIZE_BELOW_MIN_ORDER/);
  assert.doesNotMatch(gateway, /Math\.min\(requested, 5\.5\)/);
  assert.match(policy, /maxOrderUSDT: 25/);
  assert.match(wrangler, /"MAX_ORDER_USDT": "25"/);
  assert.match(relay, /quote > 25/);
});

test("higher confidence may use more notional without increasing per-trade risk cap", () => {
  assert.match(gateway, /q >= 96/);
  assert.match(gateway, /q >= 93/);
  assert.match(gateway, /q >= 90/);
  assert.match(gateway, /MAX_RISK_USDT/);
  assert.match(gateway, /riskSized = qualityRiskBudgetUSDT\(score\) \/ stopPct/);
});

test("scanner cheaply evaluates all Spot USDT symbols then rotates deep analysis", () => {
  assert.match(worker, /status==="TRADING"&&s\.quoteAsset==="USDT"&&s\.isSpotTradingAllowed/);
  assert.match(worker, /const allPool=summaries/);
  assert.match(worker, /rotateSelection\(env,allPool,newPool\)/);
  assert.match(worker, /scanPerRun: 24/);
  assert.match(worker, /eligibleUniverseSize:allPool\.length/);
});

test("automatic trading remains disabled while manual approval remains required", () => {
  assert.match(wrangler, /"AUTONOMOUS_ENABLED": "false"/);
  assert.match(wrangler, /"MANUAL_APPROVAL_ONLY": "true"/);
  assert.match(gateway, /AUTONOMOUS_BLOCKED_MANUAL_APPROVAL_ONLY/);
  assert.match(gateway, /CONFIRM BUY/);
  assert.match(gateway, /userConfirmationRequired:true/);
});
