import fs from 'node:fs';
import { scoreCandidate as scoreLarge } from '../src/strategies/regime-adaptive-momentum.mjs';
import { scoreCandidate as scoreSmall, supportedBase } from '../src/strategies/small-cap-intraday.mjs';

const BASES = [
  'https://api.binance.com',
  'https://api-gcp.binance.com',
  'https://api1.binance.com',
  'https://api2.binance.com',
  'https://api3.binance.com',
  'https://api4.binance.com',
  'https://data-api.binance.vision',
];

const TG_TOKEN = process.env.TELEGRAM_BOT_TOKEN || process.env.TELEGRAM_TOKEN || '';
const TG_CHAT_ID = process.env.TELEGRAM_CHAT_ID || process.env.TG_CHAT_ID || '';
const OUT = 'artifacts/unified-opportunity-scanner.json';

// User profile: small Spot-only account. Keep alerts selective and liquid.
const MIN_QUOTE_VOLUME_USDT = 20_000_000;
const LARGE_LIQUIDITY_CUTOFF = 150_000_000;
const MIN_ALERT_SCORE = 80;
const MAX_RISK_PCT = 5;
const MIN_RR_TO_TP2 = 1.8;
const MAX_SIGNALS = 3;
const LARGE_BASES = new Set(['BTC','ETH','BNB','SOL','XRP','ADA','DOGE','TRX','XAUT','PAXG']);

async function get(path) {
  let last;
  for (const b of BASES) {
    try {
      const r = await fetch(b + path);
      if (r.ok) return r.json();
      last = new Error(`${b} ${r.status}`);
    } catch (e) {
      last = e;
    }
  }
  throw last || new Error('NO_MARKET_DATA');
}

async function candles(symbol, interval, limit) {
  const x = await get(`/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`);
  return x
    .map(k => ({
      t: +k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5],
      q: +k[7], tbq: +k[10], closeTime: +k[6],
    }))
    .filter(x => x.closeTime < Date.now());
}

function flow(c) {
  const recent = c.slice(-4);
  const hist = c.slice(-24, -4);
  const q = recent.reduce((a, x) => a + x.q, 0);
  const tb = recent.reduce((a, x) => a + x.tbq, 0);
  const ra = q / Math.max(1, recent.length);
  const ha = hist.reduce((a, x) => a + x.q, 0) / Math.max(1, hist.length);
  return {
    takerBuyRatio: q ? tb / q : 0,
    relativeVolume: ha ? ra / ha : 0,
  };
}

function chunks(a, n) {
  const o = [];
  for (let i = 0; i < a.length; i += n) o.push(a.slice(i, i + n));
  return o;
}

function tradePlan(s) {
  const entry = Number(s.entry);
  const stop = Number(s.stop);
  if (!(entry > 0 && stop > 0 && stop < entry)) return { ...s, planOk: false, planReason: 'BAD_RISK_LEVELS' };

  const risk = entry - stop;
  const riskPct = (risk / entry) * 100;
  const tp1 = entry + 1.5 * risk;
  const tp2 = entry + 2.0 * risk;
  const rrToTp1 = (tp1 - entry) / risk;
  const rrToTp2 = (tp2 - entry) / risk;
  const planOk = (s.score || 0) >= MIN_ALERT_SCORE && riskPct <= MAX_RISK_PCT && rrToTp2 >= MIN_RR_TO_TP2;

  return {
    ...s,
    entry,
    stop,
    tp1,
    tp2,
    riskPct,
    rrToTp1,
    rrToTp2,
    planOk,
    planReason: planOk ? 'ALERT_READY' : 'RISK_OR_SCORE_FILTER',
  };
}

function fmtPrice(x) {
  if (!Number.isFinite(x)) return '—';
  if (x >= 1000) return x.toFixed(2);
  if (x >= 1) return x.toFixed(4);
  if (x >= 0.01) return x.toFixed(6);
  return x.toPrecision(6);
}

async function telegram(text) {
  if (!TG_TOKEN || !TG_CHAT_ID) return false;
  try {
    const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: TG_CHAT_ID, text }),
    });
    return r.ok;
  } catch {
    return false;
  }
}

const [tickers, books, info, btc4h, btc15] = await Promise.all([
  get('/api/v3/ticker/24hr'),
  get('/api/v3/ticker/bookTicker'),
  get('/api/v3/exchangeInfo'),
  candles('BTCUSDT', '4h', 260),
  candles('BTCUSDT', '15m', 160),
]);

const tm = new Map(tickers.map(x => [x.symbol, x]));
const bm = new Map(books.map(x => [x.symbol, x]));

const universe = (info.symbols || [])
  .filter(s => s.status === 'TRADING' && s.quoteAsset === 'USDT' && s.isSpotTradingAllowed && tm.has(s.symbol))
  .filter(s => LARGE_BASES.has(s.baseAsset) || supportedBase(s.baseAsset))
  .map(s => ({ meta: s, t: tm.get(s.symbol) }))
  .filter(x => +x.t.quoteVolume >= MIN_QUOTE_VOLUME_USDT)
  .sort((a, b) => +b.t.quoteVolume - +a.t.quoteVolume);

let ranked = [];
for (const batch of chunks(universe, 5)) {
  const rows = await Promise.all(batch.map(async ({ meta, t }) => {
    try {
      const book = bm.get(meta.symbol) || {};
      const qv = +t.quoteVolume;
      const useLarge = LARGE_BASES.has(meta.baseAsset) || qv > LARGE_LIQUIDITY_CUTOFF;

      if (!useLarge) {
        const [c15, c1h] = await Promise.all([
          candles(meta.symbol, '15m', 260),
          candles(meta.symbol, '1h', 140),
        ]);
        const f = flow(c15);
        const x = scoreSmall({
          symbol: meta.symbol,
          baseAsset: meta.baseAsset,
          c15,
          c1h,
          btc15,
          quoteVolume24h: qv,
          bid: +book.bidPrice,
          ask: +book.askPrice,
          ...f,
        });
        return { ...x, lane: 'LIQUID_ALT_INTRADAY', quoteVolume24h: qv };
      }

      const c4 = await candles(meta.symbol, '4h', 260);
      const f = flow(c4);
      const x = scoreLarge({
        symbol: meta.symbol,
        candles: c4,
        btcCandles: btc4h,
        quoteVolume24h: qv,
        bid: +book.bidPrice,
        ask: +book.askPrice,
        ...f,
      });
      return { ...x, lane: 'LARGE_LIQUID_MOMENTUM', quoteVolume24h: qv };
    } catch (e) {
      return { ok: false, symbol: meta.symbol, score: 0, reason: 'DATA_ERROR', error: String(e.message || e) };
    }
  }));
  ranked.push(...rows);
}

ranked = ranked.map(x => x.ok ? tradePlan(x) : x);
ranked.sort((a, b) => (Number(b.planOk) - Number(a.planOk)) || (Number(b.ok) - Number(a.ok)) || ((b.score || 0) - (a.score || 0)));

const signals = ranked.filter(x => x.ok && x.planOk).slice(0, MAX_SIGNALS);
const sent = [];
for (const s of signals) {
  const message = [
    `🟢 BINANCE SPOT SETUP — ${s.symbol}`,
    `Type: ${s.lane}`,
    `Score: ${s.score}/100`,
    `Entry: ${fmtPrice(s.entry)}`,
    `🛑 Stop: ${fmtPrice(s.stop)} (${s.riskPct.toFixed(2)}% risk distance)`,
    `🎯 TP1: ${fmtPrice(s.tp1)} (1.5R)`,
    `🎯 TP2: ${fmtPrice(s.tp2)} (2.0R)`,
    `24h liquidity: ${(s.quoteVolume24h / 1_000_000).toFixed(1)}M USDT`,
    `R:R to TP2: ${s.rrToTp2.toFixed(2)}`,
    `Spot only • no leverage • signal/watchlist, not guaranteed profit.`,
  ].join('\n');
  sent.push({ symbol: s.symbol, sent: await telegram(message) });
}

const out = {
  generatedAt: new Date().toISOString(),
  mode: 'BINANCE_SPOT_TELEGRAM_SCANNER',
  liveTrading: false,
  filters: {
    minQuoteVolumeUSDT: MIN_QUOTE_VOLUME_USDT,
    minAlertScore: MIN_ALERT_SCORE,
    maxRiskPct: MAX_RISK_PCT,
    minRRToTP2: MIN_RR_TO_TP2,
    maxSignalsPerRun: MAX_SIGNALS,
  },
  universeCount: universe.length,
  qualifiedSignals: signals,
  topCandidates: ranked.slice(0, 20),
  telegramConfigured: Boolean(TG_TOKEN && TG_CHAT_ID),
  telegramResults: sent,
};

fs.mkdirSync('artifacts', { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(out, null, 2) + '\n');
console.log(JSON.stringify(out, null, 2));
