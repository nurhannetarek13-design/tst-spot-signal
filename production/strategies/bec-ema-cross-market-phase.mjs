// Adapted from BEC builtin template semantics (MIT): EMA cross + SMA50/SMA200 phase filter.
// Signal-only adapter. No live execution side effects.

function ema(values, period) {
  if (!Array.isArray(values) || values.length < period) return [];
  const k = 2 / (period + 1);
  const out = new Array(values.length).fill(null);
  let seed = 0;
  for (let i = 0; i < period; i++) seed += Number(values[i]);
  out[period - 1] = seed / period;
  for (let i = period; i < values.length; i++) {
    out[i] = Number(values[i]) * k + out[i - 1] * (1 - k);
  }
  return out;
}

function sma(values, period) {
  const out = new Array(values.length).fill(null);
  if (!Array.isArray(values) || values.length < period) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += Number(values[i]);
    if (i >= period) sum -= Number(values[i - period]);
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function crossedAbove(a, b, i) {
  return i > 0 && a[i - 1] != null && b[i - 1] != null && a[i] != null && b[i] != null && a[i - 1] <= b[i - 1] && a[i] > b[i];
}

function crossedBelow(a, b, i) {
  return i > 0 && a[i - 1] != null && b[i - 1] != null && a[i] != null && b[i] != null && a[i - 1] >= b[i - 1] && a[i] < b[i];
}

export const BEC_EMA_CROSS_MARKET_PHASE = Object.freeze({
  id: 'bec_ema_cross_with_market_phases',
  source: 'jptsantossilva/BEC',
  sourceLicense: 'MIT',
  family: 'trend',
  marketType: 'spot',
  side: 'long',
  timeframe: '1h',
  validated: false,
  parameters: Object.freeze({ emaFast: 10, emaSlow: 20, smaFast: 50, smaSlow: 200 }),
});

export function evaluateBecEmaCrossMarketPhase(closes, params = BEC_EMA_CROSS_MARKET_PHASE.parameters) {
  const values = (closes || []).map(Number).filter(Number.isFinite);
  const minBars = Math.max(params.emaSlow, params.smaSlow) + 2;
  if (values.length < minBars) {
    return { action: 'NO_SIGNAL', reason: 'INSUFFICIENT_CANDLES', minBars, bars: values.length };
  }

  const fast = ema(values, params.emaFast);
  const slow = ema(values, params.emaSlow);
  const smaFast = sma(values, params.smaFast);
  const smaSlow = sma(values, params.smaSlow);
  const i = values.length - 1;
  const close = values[i];

  // Uses only the provided closed-candle series. Caller must exclude the still-open candle.
  const bullishPhase = close > smaFast[i] && close > smaSlow[i];
  const buy = bullishPhase && crossedAbove(fast, slow, i);
  const sell = crossedBelow(fast, slow, i);

  if (buy) return { action: 'BUY_SIGNAL', reason: 'EMA_CROSS_ABOVE_IN_BULLISH_PHASE', index: i };
  if (sell) return { action: 'SELL_SIGNAL', reason: 'EMA_CROSS_BELOW', index: i };
  return { action: 'NO_SIGNAL', reason: bullishPhase ? 'NO_CROSS' : 'MARKET_PHASE_FILTER' };
}
