import test from 'node:test';
import assert from 'node:assert/strict';
import { createMemoryStateStore } from '../src/execution/execution-state-store.mjs';
import { createMockBinanceSpotAdapter } from '../src/execution/mock-binance-spot-adapter.mjs';
import { createTelegramNotifier } from '../src/execution/execution-events.mjs';
import { executePaperCandidate } from '../src/execution/paper-execution-runtime.mjs';

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

test('paper candidate completes entry, OCO protection, state persistence and notifications', async () => {
  const stateStore = createMemoryStateStore();
  const exchange = createMockBinanceSpotAdapter({ priceBySymbol: { ABCUSDT: 2 } });
  const messages = [];
  const notifier = createTelegramNotifier({ sendMessage: async (text) => messages.push(text) });

  const result = await executePaperCandidate({ candidate, stateStore, exchange, notifier });
  assert.equal(result.plan.authorization, 'PAPER_ONLY');
  assert.equal(result.fill.status, 'FILLED');
  assert.equal(result.protection.status, 'ACTIVE');
  assert.equal(result.state.openPositions, 1);
  assert.equal(result.state.positions.ABCUSDT.stopPrice, 1.95);
  assert.equal(messages.length, 2);

  const orders = await exchange.listOrders();
  assert.deepEqual(orders.map(x => x.type), ['MARKET_BUY', 'OCO_SELL']);
});

test('rejected candidate creates no exchange order and persists rejection event', async () => {
  const stateStore = createMemoryStateStore();
  const exchange = createMockBinanceSpotAdapter({ priceBySymbol: { ABCUSDT: 2 } });

  await assert.rejects(
    executePaperCandidate({ candidate: { ...candidate, ok: false }, stateStore, exchange }),
    /CANDIDATE_NOT_APPROVED/,
  );

  assert.equal((await exchange.listOrders()).length, 0);
  const state = await stateStore.load();
  assert.equal(state.events.at(-1).type, 'EXECUTION_REJECTED');
});

test('paper runtime refuses a live-authorized plan even if candidate is live approved', async () => {
  const stateStore = createMemoryStateStore();
  const exchange = createMockBinanceSpotAdapter({ priceBySymbol: { ABCUSDT: 2 } });
  const livePolicy = {
    liveTrading: true,
    paperMode: false,
    maxDailyLossUsdt: 2,
    maxOpenPositions: 3,
    maxQuotePerTradeUsdt: 10,
    requireProtectiveExit: true,
  };

  await assert.rejects(
    executePaperCandidate({
      candidate: { ...candidate, liveApproved: true, reason: 'LIVE_CANDIDATE' },
      policy: livePolicy,
      stateStore,
      exchange,
    }),
    /PAPER_RUNTIME_REFUSES_LIVE_PLAN/,
  );
  assert.equal((await exchange.listOrders()).length, 0);
});
