import { buildPlanFromCandidate } from './signal-execution-adapter.mjs';
import { applyFillToState } from './spot-execution-shell.mjs';
import { appendExecutionEvent } from './execution-state-store.mjs';
import { buildExecutionEvent } from './execution-events.mjs';

export async function executePaperCandidate({
  candidate,
  policy,
  quoteAmountUsdt,
  stateStore,
  exchange,
  notifier,
} = {}) {
  if (!stateStore?.load || !stateStore?.save) throw new Error('STATE_STORE_REQUIRED');
  if (!exchange?.placeMarketBuy || !exchange?.placeOcoSell) throw new Error('EXCHANGE_ADAPTER_REQUIRED');

  let state = await stateStore.load();
  let plan;
  try {
    plan = buildPlanFromCandidate({ candidate, state, policy, quoteAmountUsdt });
  } catch (error) {
    const event = buildExecutionEvent('EXECUTION_REJECTED', {
      symbol: candidate?.symbol,
      reason: error.message,
    });
    state = appendExecutionEvent(state, event);
    await stateStore.save(state);
    if (notifier?.notify) await notifier.notify(event);
    throw error;
  }

  if (plan.liveTrading === true) throw new Error('PAPER_RUNTIME_REFUSES_LIVE_PLAN');

  const fill = await exchange.placeMarketBuy({
    symbol: plan.entry.symbol,
    quoteAmountUsdt: plan.entry.quoteAmountUsdt,
    referencePrice: candidate.entry,
  });

  const protection = await exchange.placeOcoSell({
    symbol: fill.symbol,
    quantity: fill.quantity,
    stopPrice: plan.protection.stopPrice,
    takeProfitPrice: plan.protection.takeProfitPrice,
  });

  const openedAt = new Date().toISOString();
  state = applyFillToState(state, {
    symbol: fill.symbol,
    quantity: fill.quantity,
    averagePrice: fill.averagePrice,
    at: openedAt,
  });
  state.positions = {
    ...(state.positions || {}),
    [fill.symbol]: {
      quantity: fill.quantity,
      averagePrice: fill.averagePrice,
      quoteSpent: fill.quoteSpent,
      stopPrice: protection.stopPrice,
      takeProfitPrice: protection.takeProfitPrice,
      entryOrderId: fill.orderId,
      protectionOrderListId: protection.orderListId,
      openedAt,
      maxHoldBars: Number.isFinite(Number(candidate.maxHoldBars)) ? Number(candidate.maxHoldBars) : null,
      strategy: String(candidate.strategy || 'unknown'),
      score: Number.isFinite(Number(candidate.score)) ? Number(candidate.score) : null,
      mode: 'PAPER',
    },
  };

  const entryEvent = buildExecutionEvent('PAPER_ENTRY_FILLED', fill);
  const protectionEvent = buildExecutionEvent('PROTECTION_PLACED', protection);
  state = appendExecutionEvent(state, entryEvent);
  state = appendExecutionEvent(state, protectionEvent);
  await stateStore.save(state);

  if (notifier?.notify) {
    await notifier.notify(entryEvent);
    await notifier.notify(protectionEvent);
  }

  return { plan, fill, protection, state };
}
