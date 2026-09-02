const STABLES = new Set(['USDCUSDT','FDUSDUSDT','TUSDUSDT','USDPUSDT','DAIUSDT','BUSDUSDT','USD1USDT','RLUSDUSDT','UUSDT','EURUSDT','AEURUSDT','TRYUSDT','BRLUSDT','GBPUSDT','AUDUSDT','PAXGUSDT','XAUTUSDT']);

export const STRATEGY_ID = 'REGIME_ADAPTIVE_RISK_MANAGED_MOMENTUM_V1';
export const LIVE_APPROVED = false;

export const DEFAULTS = Object.freeze({
  minQuoteVolume24h: 20_000_000,
  maxSpreadPct: 0.10,
  shortMomentumBars: 42,
  longMomentumBars: 126,
  emaTrend: 200,
  atrPeriod: 14,
  atrStopMult: 2.5,
  maxRiskUSDT: 0.20,
  maxPositionUSDT: 7,
  minTakerBuyRatio: 0.56,
  minRelativeVolume: 1.20,
  minScore: 80,
});

export function ema(values, n) {
  if (!values?.length) return [];
  const k = 2 / (n + 1);
  let v = values[0];
  return values.map(x => (v = x * k + v * (1 - k)));
}

export function atr(candles, n = 14) {
  if (!candles?.length) return [];
  const tr = candles.map((x, i) => i === 0 ? x.h - x.l : Math.max(x.h - x.l, Math.abs(x.h - candles[i-1].c), Math.abs(x.l - candles[i-1].c)));
  let v = tr[0];
  return tr.map((x, i) => (v = i < n ? tr.slice(0, i + 1).reduce((a,b)=>a+b,0)/(i+1) : (v*(n-1)+x)/n));
}

export function scoreCandidate({ symbol, candles, btcCandles, quoteVolume24h, bid, ask, takerBuyRatio, relativeVolume }, cfg = DEFAULTS) {
  const fail = reason => ({ ok:false, symbol, reason, strategy:STRATEGY_ID, liveApproved:LIVE_APPROVED });
  if (!symbol?.endsWith('USDT') || STABLES.has(symbol)) return fail('UNSUPPORTED_SYMBOL');
  if (!candles || candles.length < cfg.emaTrend + 2 || !btcCandles || btcCandles.length < cfg.emaTrend + 2) return fail('INSUFFICIENT_HISTORY');
  if (!(quoteVolume24h >= cfg.minQuoteVolume24h)) return fail('LOW_LIQUIDITY');
  if (!(bid > 0 && ask > bid)) return fail('BAD_BOOK');
  const spreadPct = ((ask - bid) / ask) * 100;
  if (spreadPct > cfg.maxSpreadPct) return fail('SPREAD_TOO_WIDE');

  const c = candles.map(x=>x.c), b = btcCandles.map(x=>x.c);
  const e200 = ema(c, cfg.emaTrend), b200 = ema(b, cfg.emaTrend);
  const i = c.length - 1, bi = b.length - 1;
  const momS = c[i] / c[i-cfg.shortMomentumBars] - 1;
  const momL = c[i] / c[i-cfg.longMomentumBars] - 1;
  const btcS = b[bi] / b[bi-cfg.shortMomentumBars] - 1;
  const btcL = b[bi] / b[bi-cfg.longMomentumBars] - 1;
  const btcRegime = b[bi] > b200[bi] && btcS > 0 && btcL > 0;
  if (!btcRegime) return fail('BTC_REGIME_OFF');
  if (!(c[i] > e200[i] && momS > 0 && momL > 0)) return fail('MOMENTUM_NOT_CONFIRMED');

  const a = atr(candles, cfg.atrPeriod);
  const atrPct = a[i] / c[i];
  const riskAdjusted = ((0.55*momS)+(0.45*momL)) / Math.max(atrPct, 1e-6);
  const orderFlowOk = Number.isFinite(takerBuyRatio) ? takerBuyRatio >= cfg.minTakerBuyRatio : false;
  const volumeOk = Number.isFinite(relativeVolume) ? relativeVolume >= cfg.minRelativeVolume : false;

  let score = 0;
  score += 25;
  score += Math.min(25, Math.max(0, riskAdjusted * 2));
  score += c[i] > e200[i] ? 15 : 0;
  score += spreadPct <= cfg.maxSpreadPct/2 ? 10 : 5;
  score += orderFlowOk ? 15 : 0;
  score += volumeOk ? 10 : 0;
  score = Math.round(Math.min(100, score));

  const entry = ask;
  const stopDistance = cfg.atrStopMult * a[i];
  const stop = entry - stopDistance;
  const qtyByRisk = cfg.maxRiskUSDT / Math.max(stopDistance, 1e-9);
  const qtyByNotional = cfg.maxPositionUSDT / entry;
  const qty = Math.max(0, Math.min(qtyByRisk, qtyByNotional));
  const notional = qty * entry;

  return {
    ok: score >= cfg.minScore && orderFlowOk && volumeOk,
    symbol,
    strategy: STRATEGY_ID,
    liveApproved: LIVE_APPROVED,
    score,
    entry,
    stop,
    qty,
    notional,
    metrics: { spreadPct, momS, momL, btcS, btcL, atrPct, riskAdjusted, takerBuyRatio, relativeVolume },
    reason: score >= cfg.minScore && orderFlowOk && volumeOk ? 'PAPER_CANDIDATE' : 'CONFIRMATION_INCOMPLETE',
  };
}
