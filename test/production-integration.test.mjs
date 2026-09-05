import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateTradeProposal } from '../production/risk-policy.mjs';
import { chooseStrategy } from '../production/regime-router.mjs';

const goodProposal = {
  marketType: 'spot',
  leverage: 1,
  positionUSDT: 10,
  riskUSDT: 0.4,
  rewardRisk: 2,
  quoteVolume24h: 50_000_000,
  spreadPct: 0.05,
  exchangeProtectionReady: true,
  exchangeFiltersValid: true,
  strategyValidated: true,
};
const goodState = {
  openPositions: 0,
  dailyLossUSDT: 0,
  consecutiveLosses: 0,
  accountReconciled: true,
};

test('small-cap policy approves only fully protected reconciled Spot proposal', () => {
  assert.equal(evaluateTradeProposal(goodProposal, goodState).approved, true);
});

test('small-cap policy fails closed on leverage', () => {
  const r = evaluateTradeProposal({ ...goodProposal, leverage: 2 }, goodState);
  assert.equal(r.approved, false);
  assert.ok(r.reasons.includes('NO_LEVERAGE'));
});

test('small-cap policy fails closed on unreconciled account', () => {
  const r = evaluateTradeProposal(goodProposal, { ...goodState, accountReconciled: false });
  assert.equal(r.approved, false);
  assert.ok(r.reasons.includes('ACCOUNT_NOT_RECONCILED'));
});

test('small-cap policy fails closed on missing exchange protection', () => {
  const r = evaluateTradeProposal({ ...goodProposal, exchangeProtectionReady: false }, goodState);
  assert.equal(r.approved, false);
  assert.ok(r.reasons.includes('PROTECTION_NOT_READY'));
});

test('router ignores unvalidated strategies even with higher score', () => {
  const result = chooseStrategy('TREND_UP', [
    { id: 'bad', family: 'trend', validated: false, qualityScore: 0.99 },
    { id: 'good', family: 'breakout', validated: true, qualityScore: 0.8 },
  ]);
  assert.equal(result.action, 'USE_STRATEGY');
  assert.equal(result.strategy.id, 'good');
});

test('unknown regime means no trade', () => {
  const result = chooseStrategy('UNKNOWN', [
    { id: 'x', family: 'trend', validated: true, qualityScore: 0.99 },
  ]);
  assert.equal(result.action, 'NO_TRADE');
});
