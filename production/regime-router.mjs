export const REGIME_STRATEGIES = Object.freeze({
  TREND_UP: ["trend", "breakout", "pullback"],
  RANGE: ["mean_reversion"],
  PANIC_EXHAUSTION: ["exhaustion"],
  UNKNOWN: [],
});

export function routeStrategies(regime, candidates = []) {
  const allowed = new Set(REGIME_STRATEGIES[regime] || []);
  return candidates
    .filter(c => c && c.validated === true && allowed.has(c.family))
    .sort((a, b) => Number(b.qualityScore || 0) - Number(a.qualityScore || 0));
}

export function chooseStrategy(regime, candidates = [], minQualityScore = 0.7) {
  const ranked = routeStrategies(regime, candidates);
  const winner = ranked.find(c => Number(c.qualityScore || 0) >= minQualityScore);
  if (!winner) return { action: "NO_TRADE", reason: "NO_VALIDATED_STRATEGY_FOR_REGIME" };
  return { action: "USE_STRATEGY", strategy: winner };
}
