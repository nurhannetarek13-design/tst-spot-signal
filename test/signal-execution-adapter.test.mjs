import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_EXECUTION_POLICY } from '../src/execution/spot-execution-shell.mjs';
import { buildPlanFromCandidate, candidateToSpotIntent } from '../src/execution/signal-execution-adapter.mjs';

const candidate = {
  ok: true,
  symbol: 'ABCUSDT',
  strategy: 'SMALL_CAP_INTRADAY_MOMENTUM_V1',
  liveApproved: false,
  score: 91,
  entry: 2,
  stop: 1.95,
  target: 2.12,
  qty: 2.5,
  notional: 5,
  reason: 'PAPER_CANDIDATE',
};

test('approved paper candidate becomes a paper execution plan', () => {
  const plan = buildPlanFromCandidate({ candidate, state: { openPositions: 0, realizedPnlTodayUsdt: 0 } });
  assert.equal(plan.authorization, 'PAPER_ONLY');
  assert.equal(plan.entry.symbol, 'ABCUSDT');
  assert.equal(plan.entry.quoteAmountUsdt, 5);
  assert.equal(plan.protection.stopPrice, 1.95);
  assert.equal(plan.candidate.score, 91);
});

test('candidate ok=false is never executable', () => {
  assert.throws(() => candidateToSpotIntent({ ...candidate, ok: false }), /CANDIDATE_NOT_APPROVED/);
});

test('cannot allocate more quote than candidate sizing allows', () => {
  assert.throws(
    () => buildPlanFromCandidate({ candidate, quoteAmountUsdt: 5.01 }),
    /QUOTE_EXCEEDS_CANDIDATE_NOTIONAL/,
  );
});

test('live policy cannot override candidate liveApproved=false', () => {
  const livePolicy = { ...DEFAULT_EXECUTION_POLICY, liveTrading: true, paperMode: false };
  assert.throws(
    () => buildPlanFromCandidate({ candidate, policy: livePolicy }),
    /CANDIDATE_NOT_LIVE_APPROVED/,
  );
});

test('explicitly live-approved candidate can pass live authorization when policy also allows it', () => {
  const livePolicy = { ...DEFAULT_EXECUTION_POLICY, liveTrading: true, paperMode: false };
  const approved = { ...candidate, liveApproved: true, reason: 'LIVE_CANDIDATE' };
  const plan = buildPlanFromCandidate({ candidate: approved, policy: livePolicy });
  assert.equal(plan.authorization, 'LIVE_AUTHORIZED');
  assert.equal(plan.liveTrading, true);
});
