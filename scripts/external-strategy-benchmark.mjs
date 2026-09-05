import fs from 'node:fs/promises';
import { evaluateBecEmaCrossMarketPhase } from '../production/strategies/bec-ema-cross-market-phase.mjs';
import { evaluateBecDualMomentum } from '../production/strategies/bec-dual-momentum-simple.mjs';

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
const HOURLY_BARS = 3000;
const DAILY_BARS = 700;
const STAKE_USDT = 10;
const BASE_FEE = 0.001;
const BASE_SLIPPAGE = 0.0005;
const API_BASES = ['https://api.binance.com', 'https://api-gcp.binance.com', 'https://data-api.binance.vision'];

const strategies = [
  {
    id: 'bec_ema_cross_with_market_phases',
    source: 'jptsantossilva/BEC',
    evaluate: ({ currentCloses }) => evaluateBecEmaCrossMarketPhase(currentCloses),
  },
  {
    id: 'bec_dual_momentum_simple',
    source: 'jptsantossilva/BEC',
    evaluate: ({ currentCloses, dailyCloses, inPosition }) => evaluateBecDualMomentum({ currentCloses, dailyCloses, inPosition }),
  },
];

function intervalMs(interval) {
  if (interval === '1h') return 60 * 60 * 1000;
  if (interval === '1d') return 24 * 60 * 60 * 1000;
  throw new Error(`Unsupported interval ${interval}`);
}

async function fetchJson(path) {
  let lastError;
  for (const base of API_BASES) {
    try {
      const response = await fetch(`${base}${path}`, { headers: { 'user-agent': 'tst-external-benchmark/1.0' } });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return await response.json();
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('Binance request failed');
}

async function fetchKlines(symbol, interval, wanted) {
  const step = intervalMs(interval);
  const now = Date.now();
  let startTime = now - (wanted + 10) * step;
  const rows = [];
  while (rows.length < wanted + 10) {
    const limit = Math.min(1000, wanted + 10 - rows.length);
    const path = `/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}&startTime=${Math.max(0, Math.floor(startTime))}`;
    const batch = await fetchJson(path);
    if (!Array.isArray(batch) || batch.length === 0) break;
    for (const k of batch) {
      rows.push({
        openTime: Number(k[0]),
        open: Number(k[1]),
        high: Number(k[2]),
        low: Number(k[3]),
        close: Number(k[4]),
        volume: Number(k[5]),
        closeTime: Number(k[6]),
      });
    }
    const last = rows.at(-1);
    if (!last) break;
    startTime = last.closeTime + 1;
    if (batch.length < limit) break;
  }
  const deduped = [...new Map(rows.map(r => [r.openTime, r])).values()]
    .filter(r => r.closeTime < now)
    .sort((a, b) => a.openTime - b.openTime);
  return deduped.slice(-wanted);
}

function dailyClosesAt(daily, closeTime) {
  const out = [];
  for (const row of daily) {
    if (row.closeTime <= closeTime) out.push(row.close);
    else break;
  }
  return out;
}

function simulateStrategy(strategy, hourly, daily) {
  const trades = [];
  let position = null;
  const closes = hourly.map(r => r.close);

  for (let i = 201; i < hourly.length - 1; i++) {
    const currentCloses = closes.slice(0, i + 1);
    const dailyCloses = dailyClosesAt(daily, hourly[i].closeTime);
    const signal = strategy.evaluate({ currentCloses, dailyCloses, inPosition: Boolean(position) });
    const next = hourly[i + 1];

    if (!position && signal.action === 'BUY_SIGNAL') {
      position = {
        signalIndex: i,
        entryIndex: i + 1,
        entryTime: next.openTime,
        entryPrice: next.open,
        entryReason: signal.reason,
      };
      continue;
    }

    if (position && signal.action === 'SELL_SIGNAL') {
      trades.push({
        ...position,
        exitSignalIndex: i,
        exitIndex: i + 1,
        exitTime: next.openTime,
        exitPrice: next.open,
        exitReason: signal.reason,
      });
      position = null;
    }
  }

  if (position) {
    const last = hourly.at(-1);
    trades.push({
      ...position,
      exitSignalIndex: hourly.length - 1,
      exitIndex: hourly.length - 1,
      exitTime: last.closeTime,
      exitPrice: last.close,
      exitReason: 'END_OF_SAMPLE_MARK',
    });
  }
  return trades;
}

function tradeNetReturn(trade, fee, slippage) {
  const entry = trade.entryPrice * (1 + slippage);
  const exit = trade.exitPrice * (1 - slippage);
  return (exit * (1 - fee)) / (entry * (1 + fee)) - 1;
}

function metrics(trades, fee=BASE_FEE, slippage=BASE_SLIPPAGE) {
  const returns = trades.map(t => tradeNetReturn(t, fee, slippage));
  const pnls = returns.map(r => STAKE_USDT * r);
  const wins = pnls.filter(x => x > 0);
  const losses = pnls.filter(x => x < 0);
  const grossProfit = wins.reduce((a, b) => a + b, 0);
  const grossLoss = losses.reduce((a, b) => a + b, 0);
  const profitFactor = grossLoss < 0 ? grossProfit / Math.abs(grossLoss) : (grossProfit > 0 ? 999 : 0);
  const expectancy = pnls.length ? pnls.reduce((a, b) => a + b, 0) / pnls.length : 0;
  const winRate = pnls.length ? wins.length / pnls.length : 0;
  let equity = 0;
  let peak = 0;
  let maxDrawdownUSDT = 0;
  for (const pnl of pnls) {
    equity += pnl;
    peak = Math.max(peak, equity);
    maxDrawdownUSDT = Math.max(maxDrawdownUSDT, peak - equity);
  }
  return {
    trades: trades.length,
    netPnlUSDT: Number(pnls.reduce((a, b) => a + b, 0).toFixed(6)),
    netReturnOn50Pct: Number((pnls.reduce((a, b) => a + b, 0) / 50 * 100).toFixed(4)),
    profitFactor: Number(profitFactor.toFixed(4)),
    expectancyUSDT: Number(expectancy.toFixed(6)),
    winRatePct: Number((winRate * 100).toFixed(2)),
    maxDrawdownUSDT: Number(maxDrawdownUSDT.toFixed(6)),
  };
}

function aggregateTradeSets(sets) {
  return sets.flat().sort((a, b) => a.entryTime - b.entryTime);
}

function promotionDecision(allMetrics, oosMetrics, stressMetrics) {
  const reasons = [];
  if (allMetrics.trades < 20) reasons.push('TOO_FEW_TRADES');
  if (allMetrics.profitFactor < 1.25) reasons.push('PF_BELOW_1_25');
  if (allMetrics.expectancyUSDT <= 0) reasons.push('EXPECTANCY_NOT_POSITIVE');
  if (allMetrics.maxDrawdownUSDT > 5) reasons.push('DRAWDOWN_ABOVE_10PCT_CAPITAL');
  if (oosMetrics.trades < 5) reasons.push('OOS_TOO_FEW_TRADES');
  if (oosMetrics.profitFactor < 1.10) reasons.push('OOS_PF_BELOW_1_10');
  if (oosMetrics.expectancyUSDT <= 0) reasons.push('OOS_EXPECTANCY_NOT_POSITIVE');
  if (stressMetrics.profitFactor < 1.0) reasons.push('STRESS_PF_BELOW_1');
  if (stressMetrics.expectancyUSDT <= 0) reasons.push('STRESS_EXPECTANCY_NOT_POSITIVE');
  return { promoted: reasons.length === 0, reasons };
}

async function main() {
  const market = {};
  for (const symbol of SYMBOLS) {
    const [hourly, daily] = await Promise.all([
      fetchKlines(symbol, '1h', HOURLY_BARS),
      fetchKlines(symbol, '1d', DAILY_BARS),
    ]);
    if (hourly.length < 1000 || daily.length < 250) throw new Error(`${symbol}: insufficient Binance data`);
    market[symbol] = { hourly, daily };
    console.log(`${symbol}: ${hourly.length} hourly / ${daily.length} daily closed candles`);
  }

  const splitTime = Math.min(...SYMBOLS.map(s => market[s].hourly[Math.floor(market[s].hourly.length * 0.70)].openTime));
  const results = [];

  for (const strategy of strategies) {
    const bySymbol = {};
    const allSets = [];
    for (const symbol of SYMBOLS) {
      const trades = simulateStrategy(strategy, market[symbol].hourly, market[symbol].daily);
      bySymbol[symbol] = {
        metrics: metrics(trades),
        trades: trades.map(t => ({ ...t, netReturn: Number(tradeNetReturn(t, BASE_FEE, BASE_SLIPPAGE).toFixed(8)) })),
      };
      allSets.push(trades.map(t => ({ ...t, symbol })));
    }
    const allTrades = aggregateTradeSets(allSets);
    const oosTrades = allTrades.filter(t => t.entryTime >= splitTime);
    const allMetrics = metrics(allTrades);
    const oosMetrics = metrics(oosTrades);
    const stressMetrics = metrics(allTrades, BASE_FEE * 2, BASE_SLIPPAGE * 2);
    const promotion = promotionDecision(allMetrics, oosMetrics, stressMetrics);
    results.push({
      id: strategy.id,
      source: strategy.source,
      validated: promotion.promoted,
      qualityScore: promotion.promoted ? Number(Math.min(1, (allMetrics.profitFactor / 2) * 0.5 + (oosMetrics.profitFactor / 2) * 0.3 + (stressMetrics.profitFactor / 2) * 0.2).toFixed(4)) : 0,
      all: allMetrics,
      oos: oosMetrics,
      stress: stressMetrics,
      promotion,
      bySymbol,
    });
  }

  const report = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    source: 'Binance public closed klines',
    symbols: SYMBOLS,
    timeframe: '1h',
    dailyConfirmation: true,
    execution: 'signal_on_closed_bar__next_bar_open',
    fees: { perSide: BASE_FEE, slippagePerSide: BASE_SLIPPAGE, stressMultiplier: 2 },
    stakeUSDT: STAKE_USDT,
    assumedCapitalUSDT: 50,
    oosSplitPct: 30,
    liveReady: false,
    results,
  };

  await fs.mkdir('validation/external', { recursive: true });
  await fs.writeFile('validation/external/benchmark-latest.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify({
    generatedAt: report.generatedAt,
    results: results.map(r => ({ id: r.id, all: r.all, oos: r.oos, stress: r.stress, promotion: r.promotion })),
  }, null, 2));
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
