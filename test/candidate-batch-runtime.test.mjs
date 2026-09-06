import test from 'node:test';
import assert from 'node:assert/strict';
import { createMemoryStateStore, emptyExecutionState } from '../src/execution/execution-state-store.mjs';
import { createMockBinanceSpotAdapter } from '../src/execution/mock-binance-spot-adapter.mjs';
import { executeRankedPaperCandidates } from '../src/execution/candidate-batch-runtime.mjs';

function candidate(symbol, score, notional = 5) {
  return {
    ok: true,
    symbol,
    strategy: 'SMALL_CAP_INTRADAY_MOMENTUM_V1',
    liveApproved: false,
    score,
    entry: 2,
    stop: 1.95,
    target: 2.12,
    qty: notional / 2,
    notional,
    reason: 'PAPER_CANDIDATE',
  };
}

test('executes highest-ranked candidate only when maxEntriesPerRun=1', async () => {
  const stateStore = createMemoryStateStore(emptyExecutionState());
  const exchange = createMockBinanceSpotAdapter();
  const out = await executeRankedPaperCandidates({
    candidates: [candidate('AAAUSDT', 82), candidate('BBBUSDT', 95)],
    stateStore,
    exchange,
    maxEntriesPerRun: 1,
  });
  assert.equal(out.executed, 1);
  assert.ok(out.finalState.positions.BBBUSDT);
  assert.equal(out.finalState.openPositions, 1);
});

test('skips a symbol already open and can execute the next candidate', async () => {
  const stateStore = createMemoryStateStore({
    ...emptyExecutionState(),
    openPositions: 1,
    positions: { AAAUSDT: { mode: 'PAPER' } },
  });
  const exchange = createMockBinanceSpotAdapter();
  const out = await executeRankedPaperCandidates({
    candidates: [candidate('AAAUSDT', 99), candidate('BBBUSDT', 90)],
    stateStore,
    exchange,
    maxEntriesPerRun: 1,
  });
  assert.equal(out.executed, 1);
  assert.ok(out.finalState.positions.BBBUSDT);
  assert.equal(out.rejected[0].reason, 'SYMBOL_ALREADY_OPEN');
});

test('refuses a live policy at the batch layer', async () => {
  const stateStore = createMemoryStateStore(emptyExecutionState());
  const exchange = createMockBinanceSpotAdapter();
  await assert.rejects(
    () => executeRankedPaperCandidates({
      candidates: [candidate('AAAUSDT', 90)],
      stateStore,
      exchange,
      policy: { liveTrading: true, paperMode: false, maxOpenPositions: 3, maxDailyLossUsdt: 2, maxQuotePerTradeUsdt: 10, requireProtectiveExit: true },
    }),
    /BATCH_RUNTIME_PAPER_ONLY/,
  );
});
