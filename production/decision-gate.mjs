import { chooseStrategy } from './regime-router.mjs';
import { evaluateTradeProposal, SMALL_CAP_POLICY } from './risk-policy.mjs';

export function evaluateDecision({ regime='UNKNOWN', candidates=[], proposal=null, state=null, minQualityScore=0.7, policy=SMALL_CAP_POLICY } = {}) {
  const selected = chooseStrategy(regime, candidates, minQualityScore);
  if (selected.action !== 'USE_STRATEGY') {
    return {
      action: 'NO_TRADE',
      stage: 'STRATEGY_SELECTION',
      reason: selected.reason,
      regime,
    };
  }

  const risk = evaluateTradeProposal({
    ...(proposal || {}),
    strategyValidated: selected.strategy.validated === true && proposal?.strategyValidated === true,
  }, state, policy);

  if (!risk.approved) {
    return {
      action: 'NO_TRADE',
      stage: 'RISK_GATE',
      regime,
      strategy: selected.strategy,
      reasons: risk.reasons,
    };
  }

  return {
    action: 'ALLOW_SIGNAL_ONLY',
    stage: 'APPROVED_FOR_SIGNAL_PIPELINE',
    liveTrading: false,
    regime,
    strategy: selected.strategy,
    proposal: {
      symbol: proposal.symbol,
      positionUSDT: Number(proposal.positionUSDT),
      riskUSDT: Number(proposal.riskUSDT),
      rewardRisk: Number(proposal.rewardRisk),
    },
  };
}
