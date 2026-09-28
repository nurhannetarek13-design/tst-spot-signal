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
