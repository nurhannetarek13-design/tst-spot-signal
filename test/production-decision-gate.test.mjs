import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateDecision } from '../production/decision-gate.mjs';

const strategy = { name: 'external-trend-v1', family: 'trend', validated: true, qualityScore: 0.82 };
const goodProposal = {
  symbol: 'BTCUSDT', marketType: 'spot', leverage: 1,
  positionUSDT: 10, riskUSDT: 0.4, rewardRisk: 2,
  quoteVolume24h: 1_000_000_000, spreadPct: 0.02,
  exchangeProtectionReady: true, exchangeFiltersValid: true,
  strategyValidated: true,
};
const goodState = { openPositions: 0, dailyLossUSDT: 0, consecutiveLosses: 0, accountReconciled: true };

test('allows signal-only for validated strategy in compatible regime', () => {
  const out = evaluateDecision({ regime: 'TREND_UP', candidates: [strategy], proposal: goodProposal, state: goodState });
  assert.equal(out.action, 'ALLOW_SIGNAL_ONLY');
  assert.equal(out.liveTrading, false);
});

test('fails closed when regime has no compatible validated strategy', () => {
  const out = evaluateDecision({ regime: 'RANGE', candidates: [strategy], proposal: goodProposal, state: goodState });
  assert.equal(out.action, 'NO_TRADE');
  assert.equal(out.stage, 'STRATEGY_SELECTION');
});

test('fails closed when account is not reconciled', () => {
  const out = evaluateDecision({ regime: 'TREND_UP', candidates: [strategy], proposal: goodProposal, state: { ...goodState, accountReconciled: false } });
  assert.equal(out.action, 'NO_TRADE');
  assert.ok(out.reasons.includes('ACCOUNT_NOT_RECONCILED'));
});

test('fails closed when exchange protection is unavailable', () => {
  const out = evaluateDecision({ regime: 'TREND_UP', candidates: [strategy], proposal: { ...goodProposal, exchangeProtectionReady: false }, state: goodState });
  assert.equal(out.action, 'NO_TRADE');
  assert.ok(out.reasons.includes('PROTECTION_NOT_READY'));
});

test('fails closed on futures or leverage', () => {
  const out = evaluateDecision({ regime: 'TREND_UP', candidates: [strategy], proposal: { ...goodProposal, marketType: 'futures', leverage: 3 }, state: goodState });
  assert.equal(out.action, 'NO_TRADE');
  assert.ok(out.reasons.includes('SPOT_ONLY'));
  assert.ok(out.reasons.includes('NO_LEVERAGE'));
});
