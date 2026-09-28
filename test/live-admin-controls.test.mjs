import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const src = await readFile(new URL("../src/buy-gateway-stable.js", import.meta.url), "utf8");

test("autonomous execution requires successful real E2E v20 proof", () => {
  const start = src.indexOf("async function processAutonomousCandidate");
  const block = src.slice(start, start + 2600);
  assert.match(block, /getState\(env,"live:e2e-result-v20"\)/);
  assert.match(block, /validRealE2EV20\(e2eProof\)/);
  assert.match(block, /AUTONOMOUS_BLOCKED_UNTIL_REAL_E2E_V20/);
});

test("admin PAUSE is an execution gate, not only a Telegram acknowledgement", () => {
  assert.match(src, /PAUSE LIVE TRADING/);
  assert.match(src, /getState\(env, "live:admin-pause"\)/);
  assert.match(src, /adminPause\?\.paused === true/);
  assert.match(src, /ADMIN_PAUSE_ACTIVE/);
  assert.match(src, /Existing protective OCO orders are NOT cancelled/);
});

test("admin RESUME remains fail-closed behind readiness gates", () => {
  assert.match(src, /RESUME LIMITED LIVE/);
  assert.match(src, /ops\?\.state==="HEALTHY"/);
  assert.match(src, /reconciliationFresh/);
  assert.match(src, /executorConfigured\(env\)/);
  assert.match(src, /unknownCount===0/);
  assert.match(src, /unprotectedCount===0/);
  assert.match(src, /validRealE2EV20\(e2eProof\)/);
  assert.match(src, /Safety\/readiness gates are not all healthy\. Trading remains paused/);
});


test("read-only production proof endpoint exposes deployed limited-live controls", () => {
  assert.match(src, /\/limited-live-readiness-proof/);
  assert.match(src, /LIMITED_LIVE_READINESS_PROOF/);
  assert.match(src, /autonomousPathRecognizesV20:true/);
  assert.match(src, /pauseImplemented:true/);
  assert.match(src, /resumeImplemented:true/);
  assert.match(src, /pauseCancelsExistingProtection:false/);
  assert.match(src, /financialAction:false/);
});


test("manual approval mode hard-blocks autonomous BUY execution", () => {
  assert.match(src, /MANUAL_APPROVAL_ONLY/);
  assert.match(src, /AUTONOMOUS_BLOCKED_MANUAL_APPROVAL_ONLY/);
  assert.match(src, /offerLatestCandidateForManualApproval/);
  assert.match(src, /manual-approval-prompt-lock/);
  assert.match(src, /userConfirmationRequired:true/);
  assert.match(src, /CONFIRM BUY/);
});

test("manual confirmation accepts verified real E2E v20 proof", () => {
  assert.match(src, /getState\(env, "live:e2e-result-v20"\)/);
  assert.match(src, /validRealE2EV20\(realE2EV20\)/);
});

test("scheduled production path offers manual approval instead of auto-buy", () => {
  const start = src.indexOf("async scheduled(event, env, ctx)");
  const block = src.slice(start, start + 1400);
  assert.match(block, /manualOnly/);
  assert.match(block, /offerLatestCandidateForManualApproval\(env\)/);
  assert.match(block, /processAutonomousCandidate\(env\)/);
});
