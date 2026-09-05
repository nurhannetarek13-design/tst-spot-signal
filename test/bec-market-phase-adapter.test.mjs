import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyBecMarketPhase } from '../production/bec-market-phase-adapter.mjs';

function seq(start, step, n=220) { return Array.from({length:n}, (_,i)=>start + step*i); }

// Strong uptrend => price>SMA50>SMA200.
test('classifies bullish as TREND_UP', () => {
  const out = classifyBecMarketPhase(seq(100, 1));
  assert.equal(out.phase, 'bullish');
  assert.equal(out.regime, 'TREND_UP');
  assert.equal(out.ready, true);
});

test('classifies bearish as UNKNOWN fail-closed', () => {
  const out = classifyBecMarketPhase(seq(400, -1));
  assert.equal(out.phase, 'bearish');
  assert.equal(out.regime, 'UNKNOWN');
});

test('rejects insufficient candles', () => {
  const out = classifyBecMarketPhase(seq(100, 1, 100));
  assert.equal(out.ready, false);
  assert.equal(out.regime, 'UNKNOWN');
  assert.equal(out.reason, 'INSUFFICIENT_CANDLES');
});
