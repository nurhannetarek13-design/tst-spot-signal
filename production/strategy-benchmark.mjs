import { BEC_EMA_CROSS_MARKET_PHASE, evaluateBecEmaCrossMarketPhase } from './strategies/bec-ema-cross-market-phase.mjs';
import { BEC_DUAL_MOMENTUM_SIMPLE, evaluateBecDualMomentum } from './strategies/bec-dual-momentum-simple.mjs';

export const EXTERNAL_STRATEGY_ADAPTERS = Object.freeze([
  {
    meta: BEC_EMA_CROSS_MARKET_PHASE,
    evaluate: ({ currentCloses }) => evaluateBecEmaCrossMarketPhase(currentCloses),
  },
  {
    meta: BEC_DUAL_MOMENTUM_SIMPLE,
    evaluate: ({ currentCloses, dailyCloses }) => evaluateBecDualMomentum({ currentCloses, dailyCloses }),
  },
]);

export function runExternalStrategySnapshot(input = {}) {
  return EXTERNAL_STRATEGY_ADAPTERS.map(({ meta, evaluate }) => {
    let result;
    try {
      result = evaluate(input);
    } catch (error) {
      result = { action: 'NO_SIGNAL', reason: 'ADAPTER_ERROR', error: String(error?.message || error) };
    }
    return {
      id: meta.id,
      source: meta.source,
      family: meta.family,
      validated: meta.validated === true,
      signalAction: result.action,
      signalReason: result.reason,
      eligibleForDecisionGate: meta.validated === true && result.action === 'BUY_SIGNAL',
    };
  });
}

export function summarizeExternalSnapshot(rows = []) {
  const total = rows.length;
  const buySignals = rows.filter(r => r.signalAction === 'BUY_SIGNAL').length;
  const validatedBuySignals = rows.filter(r => r.eligibleForDecisionGate).length;
  const adapterErrors = rows.filter(r => r.signalReason === 'ADAPTER_ERROR').length;
  return { total, buySignals, validatedBuySignals, adapterErrors };
}
