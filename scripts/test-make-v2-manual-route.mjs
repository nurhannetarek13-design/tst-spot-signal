import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../src/buy-gateway-stable.js", import.meta.url), "utf8");
assert.match(src, /manualBuyAndProtect, makeReadOnlyHeartbeat/);

const start = src.indexOf("async function executeConfirmedBuy(env, s) {");
const end = src.indexOf("\nasync function sendPromptForActive(env)", start);
assert.ok(start >= 0 && end > start, "executeConfirmedBuy block must exist");
const execute = src.slice(start, end);
assert.match(execute, /manualBuyAndProtect\(env,/);
assert.doesNotMatch(execute, /signedBinance\(/);
assert.match(execute, /MAKE_EXECUTOR_V2_READY/);
assert.match(execute, /makeV2Operational\(env\)/);
assert.match(execute, /requireFreshMakeV2ExecutionRoute\(env\)/);
assert.match(execute, /Math\.min\(requested, 5\.5\)/);

const freshPreflightStart = src.indexOf("async function requireFreshMakeV2ExecutionRoute(env)");
const freshPreflightEnd = src.indexOf("\nasync function executeConfirmedBuy(env, s)", freshPreflightStart);
assert.ok(freshPreflightStart >= 0 && freshPreflightEnd > freshPreflightStart, "fresh Make V2 preflight must exist");
const freshPreflight = src.slice(freshPreflightStart, freshPreflightEnd);
assert.match(freshPreflight, /makeReadOnlyHeartbeat\(env\)/);
assert.match(freshPreflight, /bridge:route:BUY_V2/);
assert.match(freshPreflight, /bridge:route:OCO_V2/);
assert.match(freshPreflight, /BRIDGE_AUTH_OK/);
assert.match(freshPreflight, /MAKE_V2_FRESH_PREFLIGHT_NOT_EXECUTED/);

const prepStart = src.indexOf('if (action === "PREP" && s)');
const prepEnd = src.indexOf('\n  if (action === "CANCEL")', prepStart);
assert.ok(prepStart >= 0 && prepEnd > prepStart, "PREP block must exist");
const prep = src.slice(prepStart, prepEnd);
assert.doesNotMatch(prep, /refreshBalance\(/);
assert.doesNotMatch(prep, /signedBinance\(/);
assert.match(prep, /MAKE_EXECUTOR_V2_READY/);

const ingestStart = src.indexOf("async function handleFastSignalIngest(request, env) {");
const ingestEnd = src.indexOf("\nasync function tg(env, method, payload)", ingestStart);
assert.ok(ingestStart >= 0 && ingestEnd > ingestStart, "fast ingest block must exist");
const ingest = src.slice(ingestStart, ingestEnd);
assert.doesNotMatch(ingest, /refreshBalance\(/);
assert.doesNotMatch(ingest, /signedBinance\(/);
assert.match(ingest, /makeV2Operational\(env\)/);
assert.match(ingest, /riskCappedQuote\(/);

const e2ePromptStart = src.indexOf("async function prepareManualE2EPrompt(env) {");
const e2ePromptEnd = src.indexOf("\n\nasync function handleFastSignalIngest", e2ePromptStart);
assert.ok(e2ePromptStart >= 0 && e2ePromptEnd > e2ePromptStart, "manual E2E prompt block must exist");
const e2ePrompt = src.slice(e2ePromptStart, e2ePromptEnd);
assert.doesNotMatch(e2ePrompt, /manualBuyAndProtect\(/);
assert.doesNotMatch(e2ePrompt, /signedBinance\(/);
assert.match(e2ePrompt, /CONFIRM E2E BUY/);
assert.match(e2ePrompt, /automaticExecution:false/);
assert.match(e2ePrompt, /SOLUSDT/);
assert.match(e2ePrompt, /makeV2Operational\(env\)/);

const scheduledStart = src.indexOf("async scheduled(event, env, ctx)");
assert.ok(scheduledStart >= 0, "scheduled block must exist");
const scheduled = src.slice(scheduledStart);
assert.match(scheduled, /prepareManualE2EPrompt\(env\)/);
assert.doesNotMatch(scheduled, /manualBuyAndProtect\(/);

console.log("MAKE_V2_MANUAL_ROUTE_SELFTEST_PASS");

const ops = readFileSync(new URL("../src/ops-supervisor.js", import.meta.url), "utf8");
assert.doesNotMatch(ops, /await manualBuyAndProtect\(env, input\)/);
assert.match(ops, /manualOnly: true/);
assert.match(ops, /automaticExecution: false/);
assert.match(ops, /manualExecutionAllowed:/);
assert.match(ops, /source: "MAKE_V2_READONLY_WATCHDOG"/);
assert.match(ops, /putState\(env, "bridge:health"/);
assert.match(ops, /putState\(env, "bridge:ownership"/);

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
