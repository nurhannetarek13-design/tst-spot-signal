import { DEFAULT_EXECUTION_POLICY } from './spot-execution-shell.mjs';
import { executePaperCandidate } from './paper-execution-runtime.mjs';

export async function executeRankedPaperCandidates({
  candidates = [],
  stateStore,
  exchange,
  notifier,
  policy = DEFAULT_EXECUTION_POLICY,
  maxEntriesPerRun = 1,
  minScore = 0,
} = {}) {
  if (!stateStore?.load || !stateStore?.save) throw new Error('STATE_STORE_REQUIRED');
  if (!exchange?.placeMarketBuy || !exchange?.placeOcoSell) throw new Error('EXCHANGE_ADAPTER_REQUIRED');
  if (policy.liveTrading === true || policy.paperMode !== true) throw new Error('BATCH_RUNTIME_PAPER_ONLY');

  const ranked = [...candidates]
    .filter((c) => c?.ok === true && Number(c.score || 0) >= Number(minScore || 0))
    .sort((a, b) => Number(b.score || 0) - Number(a.score || 0));

  const results = [];
  const rejected = [];
  let state = await stateStore.load();
  const occupied = new Set(Object.keys(state.positions || {}));

  for (const candidate of ranked) {
    if (results.length >= Number(maxEntriesPerRun)) break;
    if (occupied.has(String(candidate.symbol || '').toUpperCase())) {
      rejected.push({ symbol: candidate.symbol, reason: 'SYMBOL_ALREADY_OPEN' });
      continue;
    }

    state = await stateStore.load();
    if (Number(state.openPositions || 0) >= Number(policy.maxOpenPositions)) {
      rejected.push({ symbol: candidate.symbol, reason: 'MAX_OPEN_POSITIONS_REACHED' });
      break;
    }

    try {
      const result = await executePaperCandidate({
        candidate,
        policy,
        stateStore,
        exchange,
        notifier,
      });
      results.push(result);
      occupied.add(String(candidate.symbol || '').toUpperCase());
    } catch (error) {
      rejected.push({ symbol: candidate.symbol, reason: String(error?.message || error) });
    }
  }

  return {
    attempted: ranked.length,
    executed: results.length,
    results,
    rejected,
    finalState: await stateStore.load(),
  };
}
