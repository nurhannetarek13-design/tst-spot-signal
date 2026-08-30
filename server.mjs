import express from "express";

const app = express();
const API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://data-api.binance.vision",
];

const CFG = {
  capitalUSDT: 20.08,
  maxDailyLossUSDT: 2,
  maxRiskPerTradeUSDT: 0.5,
  maxPositionUSDT: 5,
  minQuoteVolume24h: 2_000_000,
  maxSpreadPct: 0.25,
  scanLimit: 24,
  feeRate: 0.001,
};

const STABLES = new Set(["USDC","FDUSD","TUSD","USDP","DAI","EUR","AEUR","TRY","BRL","BIDR","IDRT","UAH","NGN","RUB","GBP","AUD","BUSD"]);

app.get("/", (_req, res) => {
  res.json({ ok: true, service: "tst-spot-signal", mode: "SIGNAL_ONLY", liveTrading: false, endpoints: ["/health", "/scan", "/signal/BTCUSDT", "/telegram/test"], risk: CFG });
});

app.get("/telegram/test", async (_req, res) => {
  try {
    const result = await telegram("✅ TST Spot Signal is connected to Telegram.\nSignal-only mode — no live orders.");
    return res.json({ ok: true, telegram: "sent", result, liveTrading: false });
  } catch (error) {
    return res.status(500).json({ ok: false, telegram: "failed", error: String(error?.message || error), liveTrading: false });
  }
});

app.get("/health", async (_req, res) => {
  const attempts = [];
  for (const base of API_BASES) {
    try {
      const upstream = await fetch(`${base}/api/v3/time`, { headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-vercel/2.0" }, signal: AbortSignal.timeout(10_000) });
      attempts.push({ base, status: upstream.status });
      if (!upstream.ok) continue;
      const data = await upstream.json();
      return res.json({ ok: true, binance: "reachable", endpoint: base, serverTime: data.serverTime, region: process.env.VERCEL_REGION || "unknown", liveTrading: false });
    } catch (error) { attempts.push({ base, error: error.message }); }
  }
  return res.status(503).json({ ok: false, binance: "blocked", attempts, liveTrading: false });
});

app.get("/signal/:symbol", async (req, res) => {
  try {
    const symbol = String(req.params.symbol || "").toUpperCase();
    if (!/^[A-Z0-9]{4,20}USDT$/.test(symbol)) return res.status(400).json({ ok: false, error: "Use a Binance USDT spot symbol such as BTCUSDT" });
    const [ticker, book, klines] = await Promise.all([binance(`/api/v3/ticker/24hr?symbol=${symbol}`), binance(`/api/v3/ticker/bookTicker?symbol=${symbol}`), binance(`/api/v3/klines?symbol=${symbol}&interval=15m&limit=120`)]);
    return res.json(await analyze(symbol, ticker, book, klines));
  } catch (error) { return res.status(500).json({ ok: false, error: String(error?.message || error), liveTrading: false }); }
});

app.get("/scan", async (_req, res) => {
  try {
    const [tickers, books, exchangeInfo] = await Promise.all([binance("/api/v3/ticker/24hr"), binance("/api/v3/ticker/bookTicker"), binance("/api/v3/exchangeInfo")]);
    const spot = new Set(exchangeInfo.symbols.filter(s => s.status === "TRADING" && s.quoteAsset === "USDT" && s.isSpotTradingAllowed).map(s => s.symbol));
    const bookMap = new Map(books.map(b => [b.symbol, b]));
    const candidates = tickers.filter(t => spot.has(t.symbol) && bookMap.has(t.symbol)).filter(t => isAllowed(t.symbol)).map(t => ({ ...t, qv: Number(t.quoteVolume || 0), change: Number(t.priceChangePercent || 0) })).filter(t => t.qv >= CFG.minQuoteVolume24h && t.change > -15 && t.change < 35).sort((a, b) => b.qv - a.qv).slice(0, CFG.scanLimit);
    const results = [];
    for (let i = 0; i < candidates.length; i += 6) {
      const group = candidates.slice(i, i + 6);
      const analyzed = await Promise.all(group.map(async t => {
        try { const klines = await binance(`/api/v3/klines?symbol=${t.symbol}&interval=15m&limit=120`); return analyze(t.symbol, t, bookMap.get(t.symbol), klines); }
        catch (e) { return { ok: false, symbol: t.symbol, error: String(e?.message || e), decision: "WAIT", score: 0 }; }
      }));
      results.push(...analyzed);
    }
    const ranked = results.sort((a, b) => b.score - a.score);
    const buys = ranked.filter(x => x.decision === "BUY");
    return res.json({ ok: true, mode: "SIGNAL_ONLY", liveTrading: false, generatedAt: new Date().toISOString(), scanned: ranked.length, best: buys[0] || ranked[0] || null, buySignals: buys.slice(0, 5), results: ranked, risk: CFG });
  } catch (error) { return res.status(500).json({ ok: false, error: String(error?.message || error), liveTrading: false }); }
});

async function telegram(text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is missing in Vercel Environment Variables");
  if (!chatId) throw new Error("TELEGRAM_CHAT_ID is missing in Vercel Environment Variables");
  const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }), signal: AbortSignal.timeout(10_000) });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(`Telegram API error: ${data.description || r.status}`);
  return { messageId: data.result?.message_id, chatId: String(data.result?.chat?.id || chatId) };
}

async function analyze(symbol, ticker, book, rawKlines) {
  const candles = rawKlines.map(k => ({ openTime: Number(k[0]), open: Number(k[1]), high: Number(k[2]), low: Number(k[3]), close: Number(k[4]), volume: Number(k[5]), closeTime: Number(k[6]) })).filter(c => c.closeTime < Date.now());
  if (candles.length < 60) throw new Error("Not enough closed candles");
  const closes = candles.map(c => c.close), volumes = candles.map(c => c.volume), last = candles.at(-1), previous = candles.at(-2);
  const ema20 = ema(closes, 20), ema50 = ema(closes, 50), rsi14 = rsi(closes, 14), atr = atr14(candles), avgVol20 = average(volumes.slice(-21, -1));
  const volumeRatio = avgVol20 > 0 ? last.volume / avgVol20 : 0, prior20 = candles.slice(-21, -1), resistance20 = Math.max(...prior20.map(c => c.high)), support20 = Math.min(...prior20.map(c => c.low));
  const bid = Number(book.bidPrice), ask = Number(book.askPrice), mid = (bid + ask) / 2, spreadPct = mid > 0 ? ((ask - bid) / mid) * 100 : 999, change24hPct = Number(ticker.priceChangePercent || 0);
  const trendUp = last.close > ema20 && ema20 > ema50, trendDown = last.close < ema20 && ema20 < ema50, breakout = last.close > resistance20 && previous.close <= resistance20, nearBreakout = last.close >= resistance20 * 0.997, healthyMomentum = rsi14 >= 52 && rsi14 <= 68, volumeConfirm = volumeRatio >= 1.25, liquid = Number(ticker.quoteVolume || 0) >= CFG.minQuoteVolume24h, spreadOk = spreadPct <= CFG.maxSpreadPct, notChasing = change24hPct <= 25;
  let decision = "WAIT", reason = "No confirmed entry";
  if (trendUp && breakout && healthyMomentum && volumeConfirm && liquid && spreadOk && notChasing) { decision = "BUY"; reason = "15m breakout confirmed by trend, momentum and volume"; }
  else if (trendDown && rsi14 < 45) { decision = "SELL"; reason = "Spot exit signal: bearish trend and weak momentum; this is not a short signal"; }
  const entry = ask || last.close, stop = Math.max(support20, entry - 1.5 * atr), stopDistance = Math.max(entry - stop, entry * 0.005), riskQty = CFG.maxRiskPerTradeUSDT / stopDistance, budgetQty = CFG.maxPositionUSDT / entry, qty = Math.max(0, Math.min(riskQty, budgetQty)), positionUSDT = qty * entry, target1 = entry + 2 * stopDistance, estFees = positionUSDT * CFG.feeRate * 2;
  let score = 0; if (trendUp) score += 25; if (nearBreakout) score += 15; if (breakout) score += 20; if (healthyMomentum) score += 15; if (volumeConfirm) score += 15; if (spreadOk) score += 5; if (liquid) score += 5; if (!notChasing) score -= 20; score = Math.max(0, Math.min(100, score));
  return { ok: true, symbol, decision, score, reason, price: last.close, market: { change24hPct: round(change24hPct, 2), quoteVolume24hUSDT: round(Number(ticker.quoteVolume || 0), 0), spreadPct: round(spreadPct, 4) }, indicators: { ema20: round(ema20, 8), ema50: round(ema50, 8), rsi14: round(rsi14, 2), atr14: round(atr, 8), volumeRatio20: round(volumeRatio, 2), resistance20: round(resistance20, 8), support20: round(support20, 8) }, checks: { trendUp, breakout, nearBreakout, healthyMomentum, volumeConfirm, liquid, spreadOk, notChasing }, paperPlan: decision === "BUY" ? { entry: round(entry, 8), stop: round(stop, 8), target1: round(target1, 8), maxPositionUSDT: round(positionUSDT, 2), maxRiskUSDT: CFG.maxRiskPerTradeUSDT, estimatedRoundTripFeesUSDT: round(estFees, 4) } : null, liveTrading: false };
}

function isAllowed(symbol) { const base = symbol.slice(0, -4); if (STABLES.has(base)) return false; if (/UP$|DOWN$|BULL$|BEAR$/.test(base)) return false; return true; }
async function binance(path) { const attempts = []; for (const base of API_BASES) { try { const r = await fetch(`${base}${path}`, { headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-vercel/2.0" }, signal: AbortSignal.timeout(12_000) }); if (!r.ok) { attempts.push(`${base}:${r.status}`); continue; } return await r.json(); } catch (e) { attempts.push(`${base}:${e.message}`); } } throw new Error(`Binance unavailable (${attempts.join(", ")})`); }
function ema(values, period) { const k = 2 / (period + 1); let value = average(values.slice(0, period)); for (let i = period; i < values.length; i++) value = values[i] * k + value * (1 - k); return value; }
function rsi(values, period) { let gains = 0, losses = 0; for (let i = values.length - period; i < values.length; i++) { const d = values[i] - values[i - 1]; if (d >= 0) gains += d; else losses -= d; } if (losses === 0) return 100; const rs = (gains / period) / (losses / period); return 100 - 100 / (1 + rs); }
function atr14(candles) { const trs = []; for (let i = candles.length - 14; i < candles.length; i++) { const c = candles[i], p = candles[i - 1]; trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close))); } return average(trs); }
function average(values) { return values.reduce((a, b) => a + b, 0) / Math.max(values.length, 1); }
function round(value, digits = 4) { const p = 10 ** digits; return Math.round(value * p) / p; }

export default app;
