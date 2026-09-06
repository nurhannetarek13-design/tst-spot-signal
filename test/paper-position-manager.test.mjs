import test from 'node:test';
import assert from 'node:assert/strict';
import { createMemoryStateStore } from '../src/execution/execution-state-store.mjs';
import { settlePaperPositions } from '../src/execution/paper-position-manager.mjs';

test('closes paper position at target and updates pnl', async () => {
  const store = createMemoryStateStore({
    day: '2026-09-06', openPositions: 1, realizedPnlTodayUsdt: 0,
    positions: { ABCUSDT: { quantity: 2, averagePrice: 10, stopPrice: 9, takeProfitPrice: 11, openedAt: '2026-09-06T00:00:00Z', maxHoldBars: 8 } },
    events: [],
  });
  const out = await settlePaperPositions({ stateStore: store, priceBySymbol: { ABCUSDT: 11.2 }, now: Date.parse('2026-09-06T01:00:00Z'), roundTripCost: 0 });
  assert.equal(out.closed.length, 1);
  assert.equal(out.closed[0].reason, 'TARGET');
  assert.equal(out.state.openPositions, 0);
  assert.equal(out.state.positions.ABCUSDT, undefined);
  assert.equal(out.state.realizedPnlTodayUsdt, 2.4);
});

test('leaves position open when no exit condition is hit', async () => {
  const store = createMemoryStateStore({ openPositions: 1, positions: { ABCUSDT: { quantity: 1, averagePrice: 10, stopPrice: 9, takeProfitPrice: 11 } } });
  const out = await settlePaperPositions({ stateStore: store, priceBySymbol: { ABCUSDT: 10.2 }, roundTripCost: 0 });
  assert.equal(out.closed.length, 0);
  assert.equal(out.state.openPositions, 1);
});
