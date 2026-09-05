import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateBecDualMomentum } from '../production/strategies/bec-dual-momentum-simple.mjs';

function rising(n, start=100, step=0.5) {
  return Array.from({ length: n }, (_, i) => start + i * step);
}
function falling(n, start=300, step=0.5) {
  return Array.from({ length: n }, (_, i) => start - i * step);
}

test('requires current timeframe history', () => {
  const r = evaluateBecDualMomentum({ currentCloses: rising(100), dailyCloses: rising(240) });
  assert.equal(r.action, 'NO_SIGNAL');
  assert.equal(r.reason, 'CURRENT_INSUFFICIENT_CANDLES');
});

test('requires daily confirmation history', () => {
  const r = evaluateBecDualMomentum({ currentCloses: rising(240), dailyCloses: rising(100) });
  assert.equal(r.action, 'NO_SIGNAL');
  assert.equal(r.reason, 'DAILY_INSUFFICIENT_CANDLES');
});

test('emits buy only when both current and daily are bullish with positive momentum', () => {
  const r = evaluateBecDualMomentum({ currentCloses: rising(240), dailyCloses: rising(240) });
  assert.equal(r.action, 'BUY_SIGNAL');
});

test('blocks buy when daily trend is bearish', () => {
  const r = evaluateBecDualMomentum({ currentCloses: rising(240), dailyCloses: falling(240) });
  assert.equal(r.action, 'NO_SIGNAL');
  assert.equal(r.reason, 'DUAL_MOMENTUM_FILTER');
});
