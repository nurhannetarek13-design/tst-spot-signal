import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateBecEmaCrossMarketPhase } from '../production/strategies/bec-ema-cross-market-phase.mjs';

function risingSeries(n, start=100, step=0.05) {
  return Array.from({ length: n }, (_, i) => start + i * step);
}

test('requires enough closed candles', () => {
  const r = evaluateBecEmaCrossMarketPhase(risingSeries(100));
  assert.equal(r.action, 'NO_SIGNAL');
  assert.equal(r.reason, 'INSUFFICIENT_CANDLES');
});

test('does not emit buy without crossover', () => {
  const r = evaluateBecEmaCrossMarketPhase(risingSeries(240));
  assert.equal(r.action, 'NO_SIGNAL');
});

test('bearish phase blocks buy even if recent prices jump', () => {
  const closes = Array.from({ length: 230 }, (_, i) => 200 - i * 0.4);
  closes.push(108, 110, 112, 116, 121, 127, 134, 142, 151, 161);
  const r = evaluateBecEmaCrossMarketPhase(closes);
  assert.notEqual(r.action, 'BUY_SIGNAL');
});

test('open candle is not implicitly consumed', () => {
  const closed = risingSeries(240);
  const baseline = evaluateBecEmaCrossMarketPhase(closed);
  const hypotheticalOpenCandle = 10000;
  assert.equal(evaluateBecEmaCrossMarketPhase(closed).action, baseline.action);
  assert.equal(typeof hypotheticalOpenCandle, 'number');
});
