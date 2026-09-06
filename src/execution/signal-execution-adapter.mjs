import {
  DEFAULT_EXECUTION_POLICY,
  buildExecutionPlan,
} from './spot-execution-shell.mjs';

function finitePositive(value, name) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`${name} must be > 0`);
  return n;
}

export function candidateToSpotIntent(candidate, { quoteAmountUsdt } = {}) {
  if (!candidate || candidate.ok !== true) throw new Error('CANDIDATE_NOT_APPROVED');
  if (String(candidate.reason || '') !== 'PAPER_CANDIDATE' && candidate.liveApproved !== true) {
    throw new Error('CANDIDATE_REASON_NOT_EXECUTABLE');
  }

  const entryPrice = finitePositive(candidate.entry, 'candidate.entry');
  const stopPrice = finitePositive(candidate.stop, 'candidate.stop');
  const takeProfitPrice = finitePositive(candidate.target, 'candidate.target');

  const candidateNotional = Number(candidate.notional);
  const requestedQuote = quoteAmountUsdt == null ? candidateNotional : Number(quoteAmountUsdt);
  const quote = finitePositive(requestedQuote, 'quoteAmountUsdt');

  if (Number.isFinite(candidateNotional) && candidateNotional > 0 && quote > candidateNotional + 1e-9) {
    throw new Error('QUOTE_EXCEEDS_CANDIDATE_NOTIONAL');
  }

  return {
    symbol: String(candidate.symbol || '').toUpperCase(),
    entryPrice,
    stopPrice,
    takeProfitPrice,
    quoteAmountUsdt: quote,
    clientTag: `${candidate.strategy || 'unknown'}:${candidate.score ?? 'na'}`,
  };
}

export function buildPlanFromCandidate({
  candidate,
  state = {},
  policy = DEFAULT_EXECUTION_POLICY,
  quoteAmountUsdt,
} = {}) {
  const intent = candidateToSpotIntent(candidate, { quoteAmountUsdt });

  // Fail closed: a live execution policy is not sufficient on its own.
  // The strategy/candidate must also carry an explicit live approval bit.
  const requestedLive = policy.liveTrading === true && policy.paperMode === false;
  if (requestedLive && candidate.liveApproved !== true) {
    throw new Error('CANDIDATE_NOT_LIVE_APPROVED');
  }

  const plan = buildExecutionPlan({ intent, policy, state });
  return {
    ...plan,
    candidate: {
      strategy: String(candidate.strategy || 'unknown'),
      score: Number.isFinite(Number(candidate.score)) ? Number(candidate.score) : null,
      liveApproved: candidate.liveApproved === true,
      reason: String(candidate.reason || ''),
    },
  };
}
