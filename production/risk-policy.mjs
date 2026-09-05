export const SMALL_CAP_POLICY = Object.freeze({
  targetCapitalUSDT: 50,
  maxOpenPositions: 1,
  maxPositionUSDT: 10,
  maxRiskPerTradeUSDT: 0.5,
  maxDailyLossUSDT: 2,
  maxConsecutiveLosses: 3,
  minQuoteVolume24h: 20_000_000,
  maxSpreadPct: 0.10,
  minRewardRisk: 1.5,
  requireExchangeProtection: true,
  spotOnly: true,
  allowLeverage: false,
  allowMartingale: false,
  failClosed: true,
});

export function evaluateTradeProposal(proposal, state, policy = SMALL_CAP_POLICY) {
  const reasons = [];
  if (!proposal || !state) return { approved: false, reasons: ["MISSING_INPUT"] };

  const positionUSDT = Number(proposal.positionUSDT || 0);
  const riskUSDT = Number(proposal.riskUSDT || 0);
  const rewardRisk = Number(proposal.rewardRisk || 0);
  const quoteVolume24h = Number(proposal.quoteVolume24h || 0);
  const spreadPct = Number(proposal.spreadPct ?? Infinity);

  if (proposal.marketType !== "spot") reasons.push("SPOT_ONLY");
  if (proposal.leverage && Number(proposal.leverage) !== 1) reasons.push("NO_LEVERAGE");
  if (positionUSDT <= 0 || positionUSDT > policy.maxPositionUSDT) reasons.push("POSITION_LIMIT");
  if (riskUSDT <= 0 || riskUSDT > policy.maxRiskPerTradeUSDT) reasons.push("RISK_LIMIT");
  if (rewardRisk < policy.minRewardRisk) reasons.push("REWARD_RISK_TOO_LOW");
  if (quoteVolume24h < policy.minQuoteVolume24h) reasons.push("LIQUIDITY_TOO_LOW");
  if (spreadPct > policy.maxSpreadPct) reasons.push("SPREAD_TOO_WIDE");

  if (Number(state.openPositions || 0) >= policy.maxOpenPositions) reasons.push("MAX_OPEN_POSITIONS");
  if (Number(state.dailyLossUSDT || 0) >= policy.maxDailyLossUSDT) reasons.push("DAILY_LOSS_LIMIT");
  if (Number(state.consecutiveLosses || 0) >= policy.maxConsecutiveLosses) reasons.push("LOSS_STREAK_LIMIT");
  if (state.accountReconciled !== true) reasons.push("ACCOUNT_NOT_RECONCILED");
  if (policy.requireExchangeProtection && proposal.exchangeProtectionReady !== true) reasons.push("PROTECTION_NOT_READY");
  if (proposal.exchangeFiltersValid !== true) reasons.push("EXCHANGE_FILTERS_INVALID");
  if (proposal.strategyValidated !== true) reasons.push("STRATEGY_NOT_VALIDATED");

  return { approved: reasons.length === 0, reasons };
}
