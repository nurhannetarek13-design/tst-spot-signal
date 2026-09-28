import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { buildTelegramTradeProposal } from "../src/buy-gateway-stable.js";

function candidate(strategy) {
  return { id:"mock-1", symbol:"BTCUSDT", entry:123.45, target:126, stop:121.80, score:96, strategy };
}

test("Telegram proposal renders the actual candidate strategy without financial action", () => {
  const p=buildTelegramTradeProposal(candidate("LIQUIDITY_CRASH_EXHAUSTION"),18.40);
  assert.match(p.text,/BTCUSDT/);
  assert.match(p.text,/Recommended: 18\.4/);
  assert.match(p.text,/Entry ref 123\.45/);
  assert.match(p.text,/TP 126/);
  assert.match(p.text,/SL 121\.8/);
  assert.match(p.text,/Score 96\/100/);
  assert.match(p.text,/Strategy: LIQUIDITY_CRASH_EXHAUSTION/);
  assert.match(p.text,/Risk at SL/);
  assert.match(p.text,/No order is sent until you press CONFIRM BUY/);
  assert.equal(p.reply_markup.inline_keyboard[0][0].callback_data,"CONFIRM:mock-1");
  assert.equal(p.reply_markup.inline_keyboard[1][0].callback_data,"CANCEL:mock-1");
  assert.equal(p.financialAction,false);
  assert.equal(p.binanceBuySubmitted,false);
});

test("different strategies render their own identifiers; strategy is not hardcoded", () => {
  const a=buildTelegramTradeProposal(candidate("TREND_BREAKOUT"),18.40).text;
  const b=buildTelegramTradeProposal(candidate("NEW_LISTING_MOMENTUM"),18.40).text;
  assert.match(a,/Strategy: TREND_BREAKOUT/);
  assert.doesNotMatch(a,/Strategy: NEW_LISTING_MOMENTUM/);
  assert.match(b,/Strategy: NEW_LISTING_MOMENTUM/);
  assert.doesNotMatch(b,/Strategy: TREND_BREAKOUT/);
});

test("missing strategy uses explicit UNKNOWN and never fabricates a strategy", () => {
  const p=buildTelegramTradeProposal(candidate(""),18.40);
  assert.match(p.text,/Strategy: UNKNOWN/);
  assert.doesNotMatch(p.text,/FAST30_60/);
  assert.doesNotMatch(p.text,/LIQUIDITY_CRASH_EXHAUSTION/);
});

test("manual approval production safety contracts remain intact", () => {
  const src=fs.readFileSync(new URL("../src/buy-gateway-stable.js",import.meta.url),"utf8");
  assert.match(src,/AUTONOMOUS_BLOCKED_MANUAL_APPROVAL_ONLY/);
  assert.match(src,/STALE_MANUAL_CANDIDATE/);
  assert.match(src,/Already confirmed — duplicate blocked/);
  assert.match(src,/if \(action === "CANCEL"\)/);
  assert.match(src,/execution-lock:\$\{id\}/);
  assert.match(src,/No order is sent until you press CONFIRM BUY/);
  assert.match(src,/if \(manualOnly\)[\s\S]*offerLatestCandidateForManualApproval/);
});
