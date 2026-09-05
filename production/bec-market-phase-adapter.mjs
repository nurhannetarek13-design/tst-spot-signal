// Adapted from the market-phase model used by jptsantossilva/BEC (MIT License).
// Source concept: price vs SMA50/SMA200 with ROC context. This file is a clean-room JS adapter.

function sma(values, period) {
  if (!Array.isArray(values) || values.length < period) return null;
  const slice = values.slice(-period);
  if (slice.some(v => !Number.isFinite(Number(v)))) return null;
  return slice.reduce((a, b) => a + Number(b), 0) / period;
}

function roc(values, lookback) {
  if (!Array.isArray(values) || values.length <= lookback) return null;
  const now = Number(values.at(-1));
  const then = Number(values.at(-(lookback + 1)));
  if (!Number.isFinite(now) || !Number.isFinite(then) || then === 0) return null;
  return (now / then) - 1;
}

export function classifyBecMarketPhase(closes = []) {
  if (!Array.isArray(closes) || closes.length < 200) {
    return { phase: 'unknown', regime: 'UNKNOWN', ready: false, reason: 'INSUFFICIENT_CANDLES' };
  }

  const price = Number(closes.at(-1));
  const sma50 = sma(closes, 50);
  const sma200 = sma(closes, 200);
  const roc30 = roc(closes, 30);
  const roc60 = roc(closes, 60);
  if (![price, sma50, sma200, roc30, roc60].every(Number.isFinite)) {
    return { phase: 'unknown', regime: 'UNKNOWN', ready: false, reason: 'INCOMPLETE_INDICATORS' };
  }

  let phase = 'unknown';
  if (price > sma50 && price < sma200 && sma50 < sma200) phase = 'recovery';
  else if (price > sma50 && price > sma200 && sma50 < sma200) phase = 'accumulation';
  else if (price > sma50 && price > sma200 && sma50 > sma200) phase = 'bullish';
  else if (price < sma50 && price > sma200 && sma50 > sma200) phase = 'warning';
  else if (price < sma50 && price < sma200 && sma50 > sma200) phase = 'distribution';
  else if (price < sma50 && price < sma200 && sma50 < sma200) phase = 'bearish';

  const regimeMap = {
    recovery: 'TREND_UP',
    accumulation: 'TREND_UP',
    bullish: 'TREND_UP',
    warning: 'RANGE',
    distribution: 'UNKNOWN',
    bearish: 'UNKNOWN',
    unknown: 'UNKNOWN',
  };

  return {
    ready: phase !== 'unknown',
    phase,
    regime: regimeMap[phase] || 'UNKNOWN',
    metrics: { price, sma50, sma200, roc30, roc60 },
    source: 'BEC_MARKET_PHASE_MIT_ADAPTER',
  };
}
