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
  scanAllSpotUSDT: true,
  feeRate: 0.001,
};

const STABLES = new Set(["USDC","FDUSD","TUSD","USDP","DAI","EUR","AEUR","TRY","BRL","BIDR","IDRT","UAH","NGN","RUB","GBP","AUD","BUSD"]);
let lastAlertKey = null;
let lastAlertAt = 0;

app.get("/", (_req, res) => {
  res.json({
    ok: true,
    service: "tst-spot-signal",
    mode: "SIGNAL_ONLY",
    liveTrading: false,
    endpoints: ["/health", "/scan", "/signal/BTCUSDT", "/telegram/test", "/cron/scan"],
    risk: CFG,
  });
});

app.get("/telegram/test", async (_req, res) => {
  try {
    const book = await binance("/api/v3/ticker/bookTicker?symbol=BTCUSDT");
    const entry = Number(book.askPrice || book.bidPrice);
    const entryLow = entry * 0.998;
    const entryHigh = entry * 1.002;
    const stop = entry * 0.99;
    const target1 = entry * 1.02;
    const target2 = entry * 1.03;

    const text = [
      "🧪 TEST فقط — متشتريش BTC من الرسالة دي",
      "",
      "🟢 فرصة SPOT — BTC/USDT",
      "🧾 نوع الأمر: LIMIT BUY",
      "💵 Total / المبلغ: 5 USDT",
      `💲 Price / سعر الشراء: ${fmt(entry)}`,
      `✅ نطاق الدخول: ${fmt(entryLow)} → ${fmt(entryHigh)}`,
      `🛑 Stop Loss: ${fmt(stop)}`,
      `🎯 Take Profit 1: ${fmt(target1)}`,
      `🎯 Take Profit 2: ${fmt(target2)}`,
      "",
      "👇 الزر تحت يفتح BTC/USDT Spot مباشرة.",
      "بعد الفتح: Limit → Price → Total → Buy.",
      "❌ TEST فقط — مفيش إشارة شراء حقيقية هنا.",
    ].join("\n");

    const result = await telegram(text, {
      inline_keyboard: [[
        { text: "🚀 افتحي BTC/USDT على Binance Spot", url: "https://www.binance.com/en/trade/BTC_USDT?type=spot" },
      ]],
    });
    return res.json({ ok: true, telegram: "sent", result, liveTrading: false });
  } catch (error) {
    return res.status(500).json({ ok: false, telegram: "failed", error: String(error?.message || error), liveTrading: false });
  }
});

app.get("/health", async (_req, res) => {
  try {
    const data = await binance("/api/v3/time");
    return res.json({ ok: true, binance: "reachable", serverTime: data.serverTime, region: process.env.VERCEL_REGION || "unknown", liveTrading: false });
  } catch (error) {
    return res.status(503).json({ ok: false, binance: "blocked", error: String(error?.message || error), liveTrading: false });
  }
});

app.get("/signal/:symbol", async (req, res) => {
  try {
    const symbol = String(req.params.symbol || "").toUpperCase();
    if (!/^[A-Z0-9]{4,20}USDT$/.test(symbol)) return res.status(400).json({ ok: false, error: "Use a Binance USDT spot symbol such as BTCUSDT" });
    const [ticker, book, klines] = await Promise.all([
      binance(`/api/v3/ticker/24hr?symbol=${symbol}`),
      binance(`/api/v3/ticker/bookTicker?symbol=${symbol}`),
      binance(`/api/v3/klines?symbol=${symbol}&interval=15m&limit=120`),
    ]);
    return res.json(await analyze(symbol, ticker, book, klines));
  } catch (error) {
    return res.status(500).json({ ok: false, error: String(error?.message || error), liveTrading: false });
  }
});

app.get("/scan", async (_req, res) => {
  try {
    return res.json(await scanMarket());
  } catch (error) {
    return res.status(500).json({ ok: false, error: String(error?.message || error), liveTrading: false });
  }
});

app.get("/cron/scan", async (req, res) => {
  try {
    const cronSecret = process.env.CRON_SECRET;
    if (cronSecret && req.headers.authorization !== `Bearer ${cronSecret}`) {
      return res.status(401).json({ ok: false, error: "Unauthorized" });
    }

    const scan = await scanMarket();
    const actionable = scan.results.filter(x => x.decision === "BUY").sort((a, b) => b.score - a.score)[0] || null;

    if (!actionable) {
      return res.json({ ok: true, scanned: scan.scanned, alertSent: false, reason: "NO_BUY_SIGNAL", liveTrading: false });
    }

    const key = `${actionable.symbol}:${actionable.decision}:${Math.round(actionable.price * 100000)}`;
    const now = Date.now();
    if (key === lastAlertKey && now - lastAlertAt < 60 * 60 * 1000) {
      return res.json({ ok: true, scanned: scan.scanned, alertSent: false, reason: "DUPLICATE_SUPPRESSED", selected: actionable, liveTrading: false });
    }

    const p = actionable.paperPlan;
    const text = [
      `🟢 فرصة SPOT — ${actionable.symbol.replace("USDT", "/USDT")}`,
      p ? `💵 المبلغ: حتى ${p.maxPositionUSDT} USDT` : null,
      p ? `🟢 الدخول: ${fmt(p.entry)}` : null,
      p ? `🛑 الوقف: ${fmt(p.stop)}` : null,
      p ? `🎯 الهدف 1: ${fmt(p.target1)}` : null,
      p ? `🎯 الهدف 2: ${fmt(p.target2)}` : null,
      "",
      "📱 Binance → Spot → Buy",
      "❌ مش Perp / Futures ومش Long.",
      "⏱️ لو الرسالة قديمة أو السعر اتحرك بعيد عن الدخول: متدخليش.",
      `Score: ${actionable.score}/100 | RSI: ${actionable.indicators.rsi14}`,
      "Signal only — مفيش أمر اتنفذ تلقائيًا.",
    ].filter(Boolean).join("\n");

    const baseAsset = actionable.symbol.endsWith("USDT") ? actionable.symbol.slice(0, -4) : actionable.symbol;
    const pair = actionable.symbol.endsWith("USDT") ? `${baseAsset}/USDT` : actionable.symbol;
    const binanceSpotUrl = `https://www.binance.com/en/trade/${baseAsset}_USDT?type=spot`;
    const tg = await telegram(text, {
      inline_keyboard: [[
        { text: `🚀 افتحي ${pair} على Binance Spot`, url: binanceSpotUrl },
      ]],
    });
    lastAlertKey = key;
    lastAlertAt = now;
    return res.json({ ok: true, scanned: scan.scanned, alertSent: true, telegram: tg, selected: actionable, liveTrading: false });
  } catch (error) {
    return res.status(500).json({ ok: false, error: String(error?.message || error), liveTrading: false });
  }
});

async function scanMarket() {
  const [tickers, books, exchangeInfo] = await Promise.all([
    binance("/api/v3/ticker/24hr"),
    binance("/api/v3/ticker/bookTicker"),
    binance("/api/v3/exchangeInfo"),
  ]);

  const spot = new Set(exchangeInfo.symbols
    .filter(s => s.status === "TRADING" && s.quoteAsset === "USDT" && s.isSpotTradingAllowed)
    .map(s => s.symbol));
  const bookMap = new Map(books.map(b => [b.symbol, b]));

  const universe = tickers
    .filter(t => spot.has(t.symbol) && bookMap.has(t.symbol))
    .filter(t => isAllowed(t.symbol))
    .map(t => {
      const book = bookMap.get(t.symbol);
      const bid = Number(book?.bidPrice || 0);
      const ask = Number(book?.askPrice || 0);
      const mid = (bid + ask) / 2;
      const spread = mid > 0 ? ((ask - bid) / mid) * 100 : 999;
      const last = Number(t.lastPrice || 0);
      const high = Number(t.highPrice || 0);
      const nearHigh = high > 0 ? last / high : 0;
      return {
        ...t,
        qv: Number(t.quoteVolume || 0),
        change: Number(t.priceChangePercent || 0),
        spread,
        nearHigh,
      };
    });

  const safe = universe
    .filter(t => t.qv >= CFG.minQuoteVolume24h)
    .filter(t => t.change > -15 && t.change < 35)
    .filter(t => t.spread <= CFG.maxSpreadPct);

  const priority = [...safe]
    .sort((a, b) => {
      const scoreA = (a.nearHigh * 40) + (Math.max(a.change, 0) * 2) + Math.log10(Math.max(a.qv, 1));
      const scoreB = (b.nearHigh * 40) + (Math.max(b.change, 0) * 2) + Math.log10(Math.max(b.qv, 1));
      return scoreB - scoreA;
    })
    .slice(0, 60);

  const candidates = priority;

  const results = [];
  for (let i = 0; i < candidates.length; i += 12) {
    const group = candidates.slice(i, i + 12);
    const analyzed = await Promise.all(group.map(async t => {
      try {
        const klines = await binance(`/api/v3/klines?symbol=${t.symbol}&interval=15m&limit=120`);
        return analyze(t.symbol, t, bookMap.get(t.symbol), klines);
      } catch (e) {
        return { ok: false, symbol: t.symbol, error: String(e?.message || e), decision: "WAIT", score: 0 };
      }
    }));
    results.push(...analyzed);
  }

  const ranked = results.sort((a, b) => b.score - a.score);
  const buys = ranked.filter(x => x.decision === "BUY");
  return {
    ok: true,
    mode: "SIGNAL_ONLY",
    universe: "ALL_BINANCE_SPOT_USDT",
    liveTrading: false,
    generatedAt: new Date().toISOString(),
    scanned: universe.length,
    deepScanned: ranked.length,
    eligibleAfterSafety: safe.length,
    best: buys[0] || ranked[0] || null,
    buySignals: buys.slice(0, 5),
    results: ranked,
    risk: CFG,
  };
}

async function telegram(text, replyMarkup = undefined) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is missing in Vercel Environment Variables");
  if (!chatId) throw new Error("TELEGRAM_CHAT_ID is missing in Vercel Environment Variables");
  const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      disable_web_page_preview: true,
      ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
    }),
    signal: AbortSignal.timeout(10_000),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(`Telegram API error: ${data.description || r.status}`);
  return { messageId: data.result?.message_id, chatId: String(data.result?.chat?.id || chatId) };
}

async function analyze(symbol, ticker, book, rawKlines) {
  const candles = rawKlines.map(k => ({
    openTime: Number(k[0]), open: Number(k[1]), high: Number(k[2]), low: Number(k[3]), close: Number(k[4]), volume: Number(k[5]), closeTime: Number(k[6]),
  })).filter(c => c.closeTime < Date.now());
  if (candles.length < 60) throw new Error("Not enough closed candles");

  const closes = candles.map(c => c.close);
  const volumes = candles.map(c => c.volume);
  const last = candles.at(-1);
  const previous = candles.at(-2);
  const ema20 = ema(closes, 20);
  const ema50 = ema(closes, 50);
  const rsi14 = rsi(closes, 14);
  const atr = atr14(candles);
  const avgVol20 = average(volumes.slice(-21, -1));
  const volumeRatio = avgVol20 > 0 ? last.volume / avgVol20 : 0;
  const prior20 = candles.slice(-21, -1);
  const resistance20 = Math.max(...prior20.map(c => c.high));
  const support20 = Math.min(...prior20.map(c => c.low));
  const bid = Number(book.bidPrice);
  const ask = Number(book.askPrice);
  const mid = (bid + ask) / 2;
  const spreadPct = mid > 0 ? ((ask - bid) / mid) * 100 : 999;
  const change24hPct = Number(ticker.priceChangePercent || 0);

  const trendUp = last.close > ema20 && ema20 > ema50;
  const trendDown = last.close < ema20 && ema20 < ema50;
  const breakout = last.close > resistance20 && previous.close <= resistance20;
  const nearBreakout = last.close >= resistance20 * 0.997;
  const healthyMomentum = rsi14 >= 52 && rsi14 <= 68;
  const volumeConfirm = volumeRatio >= 1.25;
  const liquid = Number(ticker.quoteVolume || 0) >= CFG.minQuoteVolume24h;
  const spreadOk = spreadPct <= CFG.maxSpreadPct;
  const notChasing = change24hPct <= 25;

  let decision = "WAIT";
  let reason = "No confirmed entry";
  if (trendUp && breakout && healthyMomentum && volumeConfirm && liquid && spreadOk && notChasing) {
    decision = "BUY";
    reason = "15m breakout confirmed by trend, momentum and volume";
  } else if (trendDown && rsi14 < 45) {
    decision = "SELL";
    reason = "Spot exit signal: bearish trend and weak momentum; this is not a short signal";
  }

  const entry = ask || last.close;
  const stop = Math.max(support20, entry - 1.5 * atr);
  const stopDistance = Math.max(entry - stop, entry * 0.005);
  const riskQty = CFG.maxRiskPerTradeUSDT / stopDistance;
  const budgetQty = CFG.maxPositionUSDT / entry;
  const qty = Math.max(0, Math.min(riskQty, budgetQty));
  const positionUSDT = qty * entry;
  const target1 = entry + 2 * stopDistance;
  const target2 = entry + 3 * stopDistance;
  const estFees = positionUSDT * CFG.feeRate * 2;

  let score = 0;
  if (trendUp) score += 25;
  if (nearBreakout) score += 15;
  if (breakout) score += 20;
  if (healthyMomentum) score += 15;
  if (volumeConfirm) score += 15;
  if (spreadOk) score += 5;
  if (liquid) score += 5;
  if (!notChasing) score -= 20;
  score = Math.max(0, Math.min(100, score));

  return {
    ok: true,
    symbol,
    decision,
    score,
    reason,
    price: last.close,
    market: { change24hPct: round(change24hPct, 2), quoteVolume24hUSDT: round(Number(ticker.quoteVolume || 0), 0), spreadPct: round(spreadPct, 4) },
    indicators: { ema20: round(ema20, 8), ema50: round(ema50, 8), rsi14: round(rsi14, 2), atr14: round(atr, 8), volumeRatio20: round(volumeRatio, 2), resistance20: round(resistance20, 8), support20: round(support20, 8) },
    checks: { trendUp, breakout, nearBreakout, healthyMomentum, volumeConfirm, liquid, spreadOk, notChasing },
    signalId: `${symbol}:${last.openTime}:${round(resistance20, 8)}`,
    paperPlan: decision === "BUY" ? { entry: round(entry, 8), stop: round(stop, 8), target1: round(target1, 8), target2: round(target2, 8), maxPositionUSDT: round(positionUSDT, 2), maxRiskUSDT: CFG.maxRiskPerTradeUSDT, estimatedRoundTripFeesUSDT: round(estFees, 4) } : null,
    liveTrading: false,
  };
}

function isAllowed(symbol) {
  const base = symbol.slice(0, -4);
  if (STABLES.has(base)) return false;
  if (/UP$|DOWN$|BULL$|BEAR$/.test(base)) return false;
  return true;
}

async function binance(path) {
  const attempts = [];
  for (const base of API_BASES) {
    try {
      const r = await fetch(`${base}${path}`, {
        headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-vercel/3.0" },
        signal: AbortSignal.timeout(12_000),
      });
      if (!r.ok) { attempts.push(`${base}:${r.status}`); continue; }
      return await r.json();
    } catch (e) { attempts.push(`${base}:${e.message}`); }
  }
  throw new Error(`Binance unavailable (${attempts.join(", ")})`);
}

function ema(values, period) { const k = 2 / (period + 1); let value = average(values.slice(0, period)); for (let i = period; i < values.length; i++) value = values[i] * k + value * (1 - k); return value; }
function rsi(values, period) { let gains = 0, losses = 0; for (let i = values.length - period; i < values.length; i++) { const d = values[i] - values[i - 1]; if (d >= 0) gains += d; else losses -= d; } if (losses === 0) return 100; const rs = (gains / period) / (losses / period); return 100 - 100 / (1 + rs); }
function atr14(candles) { const trs = []; for (let i = candles.length - 14; i < candles.length; i++) { const c = candles[i], p = candles[i - 1]; trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close))); } return average(trs); }
function average(values) { return values.reduce((a, b) => a + b, 0) / Math.max(values.length, 1); }
function round(value, digits = 4) { const p = 10 ** digits; return Math.round(value * p) / p; }
function fmt(value) { if (!Number.isFinite(value)) return String(value); if (value >= 1000) return value.toFixed(2); if (value >= 1) return value.toFixed(4); return value.toPrecision(6); }

export default app;
