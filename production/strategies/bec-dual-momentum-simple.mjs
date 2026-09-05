// Adapted from BEC builtin Dual Momentum Simple semantics (MIT).
// Signal-only adapter. Requires closed candles for both current and 1d timeframes.

function sma(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += Number(values[i]);
    if (i >= period) sum -= Number(values[i - period]);
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function roc(values, period) {
  const out = new Array(values.length).fill(null);
  for (let i = period; i < values.length; i++) {
    const prev = Number(values[i - period]);
    out[i] = prev ? Number(values[i]) / prev - 1 : null;
  }
  return out;
}

function snapshot(closes, momentumWindow=60) {
  const values = (closes || []).map(Number).filter(Number.isFinite);
  const minBars = 201;
  if (values.length < minBars) return { ok: false, reason: 'INSUFFICIENT_CANDLES', bars: values.length, minBars };
  const s50 = sma(values, 50);
  const s200 = sma(values, 200);
  const r = roc(values, momentumWindow);
  const i = values.length - 1;
  return {
    ok: true,
    close: values[i],
    sma50: s50[i],
    sma200: s200[i],
    roc: r[i],
    bullish: values[i] > s50[i] && s50[i] > s200[i] && Number(r[i]) > 0,
  };
}

export const BEC_DUAL_MOMENTUM_SIMPLE = Object.freeze({
  id: 'bec_dual_momentum_simple',
  source: 'jptsantossilva/BEC',
  sourceLicense: 'MIT',
  family: 'trend',
  marketType: 'spot',
  side: 'long',
  allowedTimeframes: ['1h', '4h', '1d'],
  validated: false,
  parameters: Object.freeze({ momentumWindow: 60 }),
});

export function evaluateBecDualMomentum({ currentCloses=[], dailyCloses=[], momentumWindow=60 } = {}) {
  const current = snapshot(currentCloses, momentumWindow);
  const daily = snapshot(dailyCloses, momentumWindow);
  if (!current.ok) return { action: 'NO_SIGNAL', reason: `CURRENT_${current.reason}` };
  if (!daily.ok) return { action: 'NO_SIGNAL', reason: `DAILY_${daily.reason}` };

  if (current.bullish && daily.bullish) {
    return { action: 'BUY_SIGNAL', reason: 'DUAL_TIMEFRAME_BULLISH_MOMENTUM', current, daily };
  }
  return { action: 'NO_SIGNAL', reason: 'DUAL_MOMENTUM_FILTER', current, daily };
}
