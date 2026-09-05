import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateEconomicViability } from '../production/economic-gate.mjs';

test('rejects statistically positive but economically tiny edge', () => {
  const r = evaluateEconomicViability({ stakeUSDT: 10, expectancyRate: 0.0015, tradesPerYear: 272, worstTradeRate: -0.02 });
  assert.equal(r.approved, false);
  assert.ok(r.reasons.includes('DOLLAR_EDGE_TOO_SMALL'));
});

test('rejects strategy whose observed worst trade breaks risk budget', () => {
  const r = evaluateEconomicViability({ stakeUSDT: 10, expectancyRate: 0.005, tradesPerYear: 300, worstTradeRate: -0.0887 });
  assert.equal(r.approved, false);
  assert.ok(r.reasons.includes('WORST_TRADE_EXCEEDS_RISK_BUDGET'));
});

test('allows a meaningful controlled edge', () => {
  const r = evaluateEconomicViability({ stakeUSDT: 10, expectancyRate: 0.006, tradesPerYear: 180, worstTradeRate: -0.04 });
  assert.equal(r.approved, true);
});
