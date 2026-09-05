// Catalog adapter for BEC built-in MIT-licensed strategy templates.
// Source: jptsantossilva/BEC bec/strategy_builder/templates.py
// No strategy is production-validated merely by appearing here.

export const BEC_STRATEGIES = Object.freeze([
  { id:'bec:ema_cross', name:'EMA Cross', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['15m','1h','4h','1d','1w'] },
  { id:'bec:ema_cross_with_market_phases', name:'EMA Cross with Market Phases', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['15m','1h','4h','1d','1w'] },
  { id:'bec:market_phases', name:'Market Phases', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['15m','1h','4h','1d','1w'] },
  { id:'bec:dual_momentum_simple', name:'Dual Momentum Simple', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['1h','4h','1d'] },
  { id:'bec:hma_rsi_linreg', name:'HMA RSI LINREG', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['15m','1h','4h','1d','1w'] },
  { id:'bec:bullmarketsupportband', name:'BullMarketSupportBand', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['15m','1h','4h','1d','1w'] },
  { id:'bec:wema20', name:'WEMA20', family:'trend', validated:false, source:'BEC_MIT', allowedTimeframes:['15m','1h','4h','1d','1w'] },
]);

export function listBecCandidates({ timeframe='1h', validation={} } = {}) {
  return BEC_STRATEGIES
    .filter(s => s.allowedTimeframes.includes(timeframe))
    .map(s => {
      const v = validation[s.id] || {};
      return {
        ...s,
        validated: v.validated === true,
        qualityScore: Number(v.qualityScore || 0),
        validationRef: v.validationRef || null,
      };
    });
}
