import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_EXECUTION_POLICY,
  buildExecutionPlan,
  applyClosedTradeToState,
} from '../src/execution/spot-execution-shell.mjs';

const intent = {
  symbol: 'btcusdt',
  entryPrice: 60000,
  quoteAmountUsdt: 10,
  stopPrice: 59400,
  takeProfitPrice: 61200,
};

test('defaults to paper-only even with a valid trade intent', () => {
  const plan = buildExecutionPlan({ intent, state: { openPositions: 0, realizedPnlTodayUsdt: 0 } });
  assert.equal(plan.authorization, 'PAPER_ONLY');
  assert.equal(plan.liveTrading, false);
  assert.equal(plan.protection.type, 'OCO_AFTER_FILL');
});

test('blocks trade above configured size', () => {
  assert.throws(
    () => buildExecutionPlan({ intent: { ...intent, quoteAmountUsdt: 10.01 } }),
    /MAX_TRADE_SIZE_EXCEEDED/,
  );
});

test('blocks once daily loss cap is reached', () => {
  assert.throws(
    () => buildExecutionPlan({ intent, state: { openPositions: 0, realizedPnlTodayUsdt: -2 } }),
    /DAILY_LOSS_CAP_REACHED/,
  );
});

test('requires protective stop and target', () => {
  assert.throws(
    () => buildExecutionPlan({ intent: { ...intent, stopPrice: undefined } }),
    /stopPrice must be > 0/,
  );
});

test('live authorization requires both liveTrading=true and paperMode=false', () => {
  const policy = { ...DEFAULT_EXECUTION_POLICY, liveTrading: true, paperMode: false };
  const plan = buildExecutionPlan({ intent, policy });
  assert.equal(plan.authorization, 'LIVE_AUTHORIZED');
  assert.equal(plan.liveTrading, true);
});

test('closed trade updates daily pnl and open position count', () => {
  const next = applyClosedTradeToState(
    { openPositions: 2, realizedPnlTodayUsdt: -0.4 },
    { symbol: 'BTCUSDT', realizedPnlUsdt: 0.75, at: '2026-09-06T00:00:00Z' },
  );
  assert.equal(next.openPositions, 1);
  assert.equal(next.realizedPnlTodayUsdt, 0.35);
});
