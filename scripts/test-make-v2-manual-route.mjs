import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../src/buy-gateway-stable.js", import.meta.url), "utf8");
assert.match(src, /import \{ manualBuyAndProtect \} from "\.\/make-live-client\.js";/);

const start = src.indexOf("async function executeConfirmedBuy(env, s) {");
const end = src.indexOf("\nasync function sendPromptForActive(env)", start);
assert.ok(start >= 0 && end > start, "executeConfirmedBuy block must exist");
const execute = src.slice(start, end);
assert.match(execute, /manualBuyAndProtect\(env,/);
assert.doesNotMatch(execute, /signedBinance\(/);
assert.match(execute, /MAKE_EXECUTOR_V2_READY/);
assert.match(execute, /Math\.min\(requested, 5\.5\)/);

const prepStart = src.indexOf('if (action === "PREP" && s)');
const prepEnd = src.indexOf('\n  if (action === "CANCEL")', prepStart);
assert.ok(prepStart >= 0 && prepEnd > prepStart, "PREP block must exist");
const prep = src.slice(prepStart, prepEnd);
assert.doesNotMatch(prep, /refreshBalance\(/);
assert.doesNotMatch(prep, /signedBinance\(/);
assert.match(prep, /MAKE_EXECUTOR_V2_READY/);

console.log("MAKE_V2_MANUAL_ROUTE_SELFTEST_PASS");

const ops = readFileSync(new URL("../src/ops-supervisor.js", import.meta.url), "utf8");
assert.doesNotMatch(ops, /await manualBuyAndProtect\(env, input\)/);
assert.match(ops, /manualOnly: true/);
assert.match(ops, /automaticExecution: false/);
assert.match(ops, /manualExecutionAllowed:/);

const confirmStart = src.indexOf('if (action === "CONFIRM")');
const confirmEnd = src.indexOf('\n  return new Response("ok");', confirmStart);
assert.ok(confirmStart >= 0 && confirmEnd > confirmStart, "CONFIRM block must exist");
const confirm = src.slice(confirmStart, confirmEnd);
assert.match(confirm, /LIVE_EXECUTION_ENABLED/);
assert.match(confirm, /E2E_ARMED/);
assert.match(confirm, /cutover:e2e:result/);
assert.match(confirm, /manual: true/);
assert.match(confirm, /automaticExecution: false/);
assert.match(execute, /live:unknown-orders/);

console.log("MANUAL_E2E_ARMING_SELFTEST_PASS");
