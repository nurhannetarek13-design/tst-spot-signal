export const SMALL_CAP_ECONOMICS = Object.freeze({
  capitalUSDT: 50,
  defaultStakeUSDT: 10,
  maxDailyLossUSDT: 2,
  minExpectedProfitPerTradeUSDT: 0.03,
  minProjectedAnnualProfitUSDT: 8,
  maxObservedLossPerTradeUSDT: 0.5,
});

export function evaluateEconomicViability(metrics = {}, config = SMALL_CAP_ECONOMICS) {
  const stake = Number(metrics.stakeUSDT ?? config.defaultStakeUSDT);
  const expectancyRate = Number(metrics.expectancyRate ?? 0);
  const tradesPerYear = Number(metrics.tradesPerYear ?? 0);
  const worstLossRate = Math.abs(Math.min(0, Number(metrics.worstTradeRate ?? 0)));
  const expectedProfitPerTradeUSDT = stake * expectancyRate;
  const projectedAnnualProfitUSDT = expectedProfitPerTradeUSDT * tradesPerYear;
  const observedWorstLossUSDT = stake * worstLossRate;
  const reasons = [];

  if (!(expectancyRate > 0)) reasons.push('NON_POSITIVE_EXPECTANCY');
  if (expectedProfitPerTradeUSDT < config.minExpectedProfitPerTradeUSDT) reasons.push('DOLLAR_EDGE_TOO_SMALL');
  if (projectedAnnualProfitUSDT < config.minProjectedAnnualProfitUSDT) reasons.push('ANNUAL_DOLLAR_RETURN_TOO_SMALL');
  if (observedWorstLossUSDT > config.maxObservedLossPerTradeUSDT) reasons.push('WORST_TRADE_EXCEEDS_RISK_BUDGET');

  return {
    approved: reasons.length === 0,
    reasons,
    expectedProfitPerTradeUSDT,
    projectedAnnualProfitUSDT,
    observedWorstLossUSDT,
    stakeUSDT: stake,
  };
}
