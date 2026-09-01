import express from "express";

const app = express();
app.use(express.text({ type: "application/json", limit: "16kb" }));
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
  maxPositionUSDT: 7,
  minQuoteVolume24h: 5_000_000,
  maxSpreadPct: 0.15,
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
    const amount = 5 / entry;
    const entryLow = entry * 0.998;
    const entryHigh = entry * 1.002;
    const stop = entry * 0.99;
    const target1 = entry * 1.02;
    const target2 = entry * 1.03;

    const text = [
      "🧪 TEST فقط — متشتريش BTC من الرسالة دي",
      "",
      "🟢 BUY NOW — BTC/USDT — SPOT",
      "",
      "━━━ اكتبي في Binance بالظبط ━━━",
      "",
      "1️⃣ PRICE / السعر",
      `${fmt(entry)}`,
      "",
      "2️⃣ AMOUNT / الكمية",
      `${fmt(amount)} BTC`,
      "",
      "3️⃣ TOTAL / المبلغ",
      "5 USDT",
      "",
      "✅ بعد ما تحطيهم: دوسي BUY BTC",
      "",
      "━━━ بعد الشراء ━━━",
      `🛑 STOP LOSS: ${fmt(stop)}`,
      `🎯 TAKE PROFIT 1: ${fmt(target1)}`,
      `🎯 TAKE PROFIT 2: ${fmt(target2)}`,
      "",
      "━━━ شرط الدخول ━━━",
      "السعر الحالي لازم يكون بين:",
      `${fmt(entryLow)}  →  ${fmt(entryHigh)}`,
      "",
      "👇 الزر تحت يفتح BTC/USDT Spot مباشرة.",
      "❌ TEST فقط — دي مش إشارة شراء حقيقية.",
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

app.get("/executor/status", async (_req, res) => {
  const cfg = executorConfig();
  return res.json({
    ok: true,
    executor: "VERCEL_SPOT_EXECUTOR",
    apiConnected: hasBinanceSigningKey(),
    liveTradingEnabled: cfg.enabled,
    tradeUSDT: cfg.tradeUSDT,
    maxOpenPositions: cfg.maxOpenPositions,
    reserveUSDT: cfg.reserveUSDT,
    maxRiskUSDT: cfg.maxRiskUSDT,
    staticEgressRequiredForBinanceWhitelist: true,
    region: process.env.VERCEL_REGION || "unknown",
  });
});

app.post("/execute/spot-buy", async (req, res) => {
  try {
    const raw = typeof req.body === "string" ? req.body : "";
    if (!raw) return res.status(400).json({ ok: false, status: "EMPTY_BODY" });

    const auth = await verifyExecutorRelay(req, raw);
    if (!auth.ok) return res.status(401).json({ ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const body = JSON.parse(raw);
    const symbol = String(body.symbol || "").toUpperCase();
    if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) {
      return res.status(400).json({ ok: false, status: "BAD_SYMBOL" });
    }

    const result = await executeSpotOrder(symbol);
    return res.status(result.ok ? 200 : 409).json(result);
  } catch (error) {
    return res.status(500).json({ ok: false, status: "EXECUTOR_ERROR", error: String(error?.message || error) });
  }
});



app.post("/executor/preflight", async (req, res) => {
  try {
    const raw = typeof req.body === "string" ? req.body : "";
    if (!raw) return res.status(400).json({ ok: false, status: "EMPTY_BODY", orderPlaced: false });

    const auth = await verifyExecutorRelay(req, raw);
    if (!auth.ok) return res.status(401).json({ ok: false, status: "UNAUTHORIZED", reason: auth.reason, orderPlaced: false });

    const cfg = executorConfig();
    if (!hasBinanceSigningKey()) {
      return res.status(409).json({ ok: false, status: "API_NOT_CONNECTED", orderPlaced: false });
    }

    const body = JSON.parse(raw);
    const symbol = String(body.symbol || "BTCUSDT").toUpperCase();
    const requestedQuote = Number(body.quoteOrderQty || CFG.maxPositionUSDT);
    const quoteOrderQty = Math.min(CFG.maxPositionUSDT, Math.max(0, requestedQuote)).toFixed(2);
    if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) {
      return res.status(400).json({ ok: false, status: "BAD_SYMBOL", orderPlaced: false });
    }

    const account = await signedBinance("GET", "/api/v3/account", {});
    if (!account.canTrade) {
      return res.status(409).json({ ok: false, status: "ACCOUNT_CANNOT_TRADE", orderPlaced: false });
    }
    const freeUSDT = Number((account.balances || []).find(b => b.asset === "USDT")?.free || 0);

    await signedBinance("POST", "/api/v3/order/test", {
      symbol,
      side: "BUY",
      type: "MARKET",
      quoteOrderQty,
      newClientOrderId: `TSTP${crypto.randomUUID().replaceAll("-", "").slice(0, 8)}`,
      computeCommissionRates: false,
    });

    return res.json({
      ok: true,
      status: "PREFLIGHT_PASSED",
      symbol,
      testedQuoteUSDT: Number(quoteOrderQty),
      freeUSDT: round(freeUSDT, 2),
      liveTradingEnabled: cfg.enabled,
      orderPlaced: false,
      fundsUsed: false,
      note: "Binance order/test accepted the signed Spot order parameters. No order was placed.",
    });
  } catch (error) {
    return res.status(409).json({
      ok: false,
      status: "PREFLIGHT_FAILED",
      reason: String(error?.message || error),
      orderPlaced: false,
      fundsUsed: false,
    });
  }
});

app.post("/executor/discover-open-positions", async (req, res) => {
  try {
    const raw = typeof req.body === "string" ? req.body : "";
    if (!raw) return res.status(400).json({ ok: false, status: "EMPTY_BODY" });
    const auth = await verifyExecutorRelay(req, raw);
    if (!auth.ok) return res.status(401).json({ ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const openOrders = await signedBinance("GET", "/api/v3/openOrders", {});
    const symbols = [...new Set((openOrders || [])
      .filter(o => o.side === "SELL" && /^TST[TS]/.test(String(o.clientOrderId || "")))
      .map(o => o.symbol))];
    const positions = [];
    for (const symbol of symbols) {
      const orders = await signedBinance("GET", "/api/v3/allOrders", { symbol, limit: 1000 });
      const entry = (orders || [])
        .filter(o => o.side === "BUY" && o.status === "FILLED" && String(o.clientOrderId || "").startsWith("TSTB"))
        .sort((a, b) => Number(b.updateTime || 0) - Number(a.updateTime || 0))[0];
      if (entry) {
        positions.push({
          symbol,
          entryOrderId: Number(entry.orderId),
          entryPrice: Number(entry.executedQty || 0) > 0 ? Number(entry.cummulativeQuoteQty || 0) / Number(entry.executedQty) : 0,
          quantity: entry.executedQty,
          quoteSpentUSDT: entry.cummulativeQuoteQty,
          openedAt: Number(entry.time || entry.updateTime || Date.now()),
        });
      }
    }
    return res.json({ ok: true, status: "DISCOVERY_OK", positions });
  } catch (error) {
    return res.status(503).json({ ok: false, status: "DISCOVERY_FAILED", reason: String(error?.message || error) });
  }
});

app.post("/executor/position-status", async (req, res) => {
  try {
    const raw = typeof req.body === "string" ? req.body : "";
    if (!raw) return res.status(400).json({ ok: false, status: "EMPTY_BODY" });
    const auth = await verifyExecutorRelay(req, raw);
    if (!auth.ok) return res.status(401).json({ ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const body = JSON.parse(raw);
    const symbol = String(body.symbol || "").toUpperCase();
    const entryOrderId = Number(body.entryOrderId);
    if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol) || !(entryOrderId > 0)) {
      return res.status(400).json({ ok: false, status: "BAD_POSITION_REFERENCE" });
    }

    const entryOrder = await signedBinance("GET", "/api/v3/order", { symbol, orderId: entryOrderId });
    const orders = await signedBinance("GET", "/api/v3/allOrders", {
      symbol,
      startTime: Math.max(0, Number(entryOrder.time || 0) - 60_000),
      limit: 1000,
    });
    const exitOrder = (orders || [])
      .filter(o => o.side === "SELL" && o.status === "FILLED" && Number(o.updateTime || 0) >= Number(entryOrder.updateTime || entryOrder.time || 0))
      .filter(o => /^TST[TSX]/.test(String(o.clientOrderId || "")))
      .sort((a, b) => Number(a.updateTime || 0) - Number(b.updateTime || 0))[0];

    if (!exitOrder) {
      return res.json({ ok: true, status: "OPEN", symbol, entryOrderId, closed: false });
    }

    const [entryTrades, exitTrades] = await Promise.all([
      signedBinance("GET", "/api/v3/myTrades", { symbol, orderId: entryOrderId, limit: 1000 }),
      signedBinance("GET", "/api/v3/myTrades", { symbol, orderId: exitOrder.orderId, limit: 1000 }),
    ]);
    const entryQty = (entryTrades || []).reduce((n, t) => n + Number(t.qty || 0), 0);
    const exitQty = (exitTrades || []).reduce((n, t) => n + Number(t.qty || 0), 0);
    const entryQuote = (entryTrades || []).reduce((n, t) => n + Number(t.quoteQty || Number(t.price || 0) * Number(t.qty || 0)), 0);
    const exitQuote = (exitTrades || []).reduce((n, t) => n + Number(t.quoteQty || Number(t.price || 0) * Number(t.qty || 0)), 0);
    const entryAvg = entryQty > 0 ? entryQuote / entryQty : 0;
    const exitAvg = exitQty > 0 ? exitQuote / exitQty : 0;
    const feesUSDT = await tradeFeesUSDT([...entryTrades, ...exitTrades], symbol, entryAvg, exitAvg);
    const netPnlUSDT = exitQuote - entryQuote - feesUSDT;
    const reason = String(exitOrder.clientOrderId || "").startsWith("TSTT")
      ? "TAKE_PROFIT"
      : String(exitOrder.clientOrderId || "").startsWith("TSTS")
        ? "STOP_LOSS"
        : "SAFETY_CLOSE";

    return res.json({
      ok: true,
      status: "CLOSED",
      closed: true,
      symbol,
      entryOrderId,
      exitOrderId: exitOrder.orderId,
      reason,
      entryPrice: fmt(entryAvg),
      exitPrice: fmt(exitAvg),
      quantity: floorToStep(exitQty, 0.00000001),
      entryQuoteUSDT: round(entryQuote, 5),
      exitQuoteUSDT: round(exitQuote, 5),
      feesUSDT: round(feesUSDT, 5),
      netPnlUSDT: round(netPnlUSDT, 5),
      closedAt: Number(exitOrder.updateTime || Date.now()),
    });
  } catch (error) {
    return res.status(503).json({ ok: false, status: "POSITION_STATUS_FAILED", reason: String(error?.message || error) });
  }
});

async function tradeFeesUSDT(trades, symbol, entryAvg, exitAvg) {
  const baseAsset = symbol.slice(0, -4);
  let total = 0;
  for (const trade of trades || []) {
    const amount = Number(trade.commission || 0);
    const asset = String(trade.commissionAsset || "");
    if (!(amount > 0)) continue;
    if (asset === "USDT") total += amount;
    else if (asset === baseAsset) total += amount * (trade.isBuyer ? entryAvg : exitAvg);
    else {
      try {
        const px = await binance(`/api/v3/ticker/price?symbol=${asset}USDT`);
        total += amount * Number(px.price || 0);
      } catch {
        // Unknown fee asset is deliberately not guessed.
      }
    }
  }
  return total;
}

app.post("/executor/account-status", async (req, res) => {
  try {
    const raw = typeof req.body === "string" ? req.body : "";
    if (!raw) return res.status(400).json({ ok: false, status: "EMPTY_BODY" });

    const auth = await verifyExecutorRelay(req, raw);
    if (!auth.ok) return res.status(401).json({ ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const cfg = executorConfig();
    if (!hasBinanceSigningKey()) {
      return res.status(409).json({ ok: false, status: "API_NOT_CONNECTED" });
    }

    const [account, prices, openOrders] = await Promise.all([
      signedBinance("GET", "/api/v3/account", {}),
      binance("/api/v3/ticker/price"),
      signedBinance("GET", "/api/v3/openOrders", {}),
    ]);
    const priceMap = new Map((prices || []).map(p => [p.symbol, Number(p.price || 0)]));
    const dollarStables = new Set(["USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD"]);
    let equityUSDT = 0;
    for (const balance of account.balances || []) {
      const asset = String(balance.asset || "");
      const total = Number(balance.free || 0) + Number(balance.locked || 0);
      if (!(total > 0)) continue;
      if (asset === "USDT" || dollarStables.has(asset)) equityUSDT += total;
      else {
        const px = priceMap.get(`${asset}USDT`) || 0;
        if (px > 0) equityUSDT += total * px;
      }
    }

    const usdt = (account.balances || []).find(b => b.asset === "USDT") || {};
    const freeUSDT = Number(usdt.free || 0);
    const lockedUSDT = Number(usdt.locked || 0);
    const botOpenSymbols = [...new Set((openOrders || [])
      .filter(o => String(o.clientOrderId || "").startsWith("TST"))
      .map(o => o.symbol))];

    return res.json({
      ok: true,
      status: "ACCOUNT_READ_OK",
      freeUSDT: round(freeUSDT, 2),
      lockedUSDT: round(lockedUSDT, 2),
      equityUSDT: round(equityUSDT, 2),
      availableAfterReserveUSDT: round(Math.max(0, freeUSDT - cfg.reserveUSDT), 2),
      botOpenPositions: botOpenSymbols.length,
      maxOpenPositions: cfg.maxOpenPositions,
      liveTradingEnabled: cfg.enabled,
      readOnlyCheck: true,
    });
  } catch (error) {
    return res.status(503).json({
      ok: false,
      status: "ACCOUNT_READ_FAILED",
      reason: String(error?.message || error),
      readOnlyCheck: true,
    });
  }
});

app.get("/signal/:symbol", async (req, res) => {
  try {
    const symbol = String(req.params.symbol || "").toUpperCase();
    if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) return res.status(400).json({ ok: false, error: "Use a Binance USDT spot symbol such as BTCUSDT" });
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
    const baseAsset = actionable.symbol.endsWith("USDT") ? actionable.symbol.slice(0, -4) : actionable.symbol;
    const pair = actionable.symbol.endsWith("USDT") ? `${baseAsset}/USDT` : actionable.symbol;
    const entry = Number(p?.entry);
    const amount = p ? Number(p.maxPositionUSDT) / entry : 0;
    const text = [
      `🟢 BUY NOW — ${pair} — SPOT`,
      "",
      "━━━ اكتبي في Binance بالظبط ━━━",
      "",
      "1️⃣ PRICE / السعر",
      p ? `${fmt(entry)}` : "-",
      "",
      "2️⃣ AMOUNT / الكمية",
      p ? `${fmt(amount)} ${baseAsset}` : "-",
      "",
      "3️⃣ TOTAL / المبلغ",
      p ? `${p.maxPositionUSDT} USDT` : "-",
      "",
      `✅ بعد ما تحطيهم: دوسي BUY ${baseAsset}`,
      "",
      "━━━ بعد الشراء ━━━",
      p ? `🛑 STOP LOSS: ${fmt(p.stop)}` : null,
      p ? `🎯 TAKE PROFIT 1: ${fmt(p.target1)}` : null,
      p ? `🎯 TAKE PROFIT 2: ${fmt(p.target2)}` : null,
      "",
      "❌ لو ظهر Perp / Futures / Long / Short: متدخليش.",
      "⏱️ لو الرسالة قديمة أو السعر اتحرك بعيد عن الدخول: متدخليش.",
    ].filter(Boolean).join("\n");

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

function executorConfig() {
  const num = (value, fallback, min, max) => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
  };
  return {
    enabled: ["1","true","yes","on"].includes(String(process.env.LIVE_TRADING || "").toLowerCase()),
    // Hard safety limits: environment variables may only reduce these values.
    tradeUSDT: num(process.env.TRADE_USDT, CFG.maxPositionUSDT, 1, CFG.maxPositionUSDT),
    maxOpenPositions: Math.floor(num(process.env.MAX_OPEN_POSITIONS, 3, 1, 20)),
    reserveUSDT: num(process.env.RESERVE_USDT, 2, 0, 100000),
    maxRiskUSDT: num(process.env.MAX_RISK_USDT, CFG.maxRiskPerTradeUSDT, 0.01, CFG.maxRiskPerTradeUSDT),
    minNetRewardRisk: 2.5,
  };
}

async function verifyExecutorRelay(req, raw) {
  const secret = process.env.TELEGRAM_BOT_TOKEN;
  if (!secret) return { ok: false, reason: "Executor relay secret missing" };

  const ts = String(req.headers["x-executor-timestamp"] || "");
  const signature = String(req.headers["x-executor-signature"] || "");
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) {
    return { ok: false, reason: "Expired relay request" };
  }

  const expected = await hmacHex(secret, `${ts}.${raw}`);
  if (!timingSafeEqualHex(expected, signature)) return { ok: false, reason: "Bad relay signature" };
  return { ok: true };
}

function timingSafeEqualHex(a, b) {
  if (!/^[a-f0-9]+$/i.test(a) || !/^[a-f0-9]+$/i.test(b) || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function executeSpotOrder(symbol) {
  const cfg = executorConfig();
  if (!cfg.enabled) return { ok: false, status: "LIVE_DISABLED", reason: "LIVE_TRADING is not enabled." };
  if (!hasBinanceSigningKey()) {
    return { ok: false, status: "API_NOT_CONNECTED", reason: "Binance trading API keys are not connected." };
  }

  const [ticker, book, klines, info, hourlyKlines, depth] = await Promise.all([
    binance(`/api/v3/ticker/24hr?symbol=${symbol}`),
    binance(`/api/v3/ticker/bookTicker?symbol=${symbol}`),
    binance(`/api/v3/klines?symbol=${symbol}&interval=15m&limit=120`),
    binance(`/api/v3/exchangeInfo?symbol=${symbol}`),
    binance(`/api/v3/klines?symbol=${symbol}&interval=1h&limit=80`),
    binance(`/api/v3/depth?symbol=${symbol}&limit=100`),
  ]);
  const fresh = await analyze(symbol, ticker, book, klines);
  if (!fresh.ok || fresh.decision !== "BUY" || !fresh.paperPlan || !fresh.checks?.successfulRetest) {
    return { ok: false, status: "SIGNAL_NO_LONGER_VALID", reason: "A completed 15m breakout and a separate successful retest are both required." };
  }

  const closedHourly = hourlyKlines
    .filter(k => Number(k[6]) < Date.now())
    .map(k => Number(k[4]));
  const hourlyEma20 = ema(closedHourly, 20);
  const hourlyEma50 = ema(closedHourly, 50);
  const hourlyTrendUp = closedHourly.at(-1) > hourlyEma20 && hourlyEma20 > hourlyEma50;
  const bid = Number(book.bidPrice || 0);
  const askNow = Number(book.askPrice || 0);
  const mid = (bid + askNow) / 2;
  const spreadPct = mid > 0 ? ((askNow - bid) / mid) * 100 : 999;
  const bidDepth05 = (depth.bids || [])
    .filter(([price]) => Number(price) >= mid * 0.995)
    .reduce((sum, [price, qty]) => sum + Number(price) * Number(qty), 0);
  const askDepth05 = (depth.asks || [])
    .filter(([price]) => Number(price) <= mid * 1.005)
    .reduce((sum, [price, qty]) => sum + Number(price) * Number(qty), 0);
  if (!hourlyTrendUp) {
    return { ok: false, status: "HOURLY_TREND_NOT_CONFIRMED", reason: "1h trend is not strictly rising above EMA20 and EMA50.", orderPlaced: false };
  }
  if (spreadPct > CFG.maxSpreadPct || bidDepth05 < 10_000 || askDepth05 < 10_000) {
    return {
      ok: false,
      status: "LIQUIDITY_NOT_CONFIRMED",
      reason: `Spread/depth failed: spread ${round(spreadPct, 3)}%, bid depth ${round(bidDepth05, 0)}, ask depth ${round(askDepth05, 0)} USDT.`,
      orderPlaced: false,
    };
  }

  const symbolInfo = info.symbols?.[0];
  if (!symbolInfo || symbolInfo.status !== "TRADING" || !symbolInfo.isSpotTradingAllowed) {
    return { ok: false, status: "SPOT_UNAVAILABLE", reason: "Pair is not available for Spot trading." };
  }

  const [account, openOrders] = await Promise.all([
    signedBinance("GET", "/api/v3/account", {}),
    signedBinance("GET", "/api/v3/openOrders", {}),
  ]);
  if (!account.canTrade) return { ok: false, status: "ACCOUNT_CANNOT_TRADE", reason: "Binance account cannot trade." };

  const botOrders = (openOrders || []).filter(o => String(o.clientOrderId || "").startsWith("TST"));
  const openSymbols = [...new Set(botOrders.map(o => o.symbol))];
  if (openSymbols.includes(symbol)) {
    return { ok: false, status: "SYMBOL_ALREADY_OPEN", reason: "A bot-managed position is already open on this pair." };
  }
  if (openSymbols.length >= cfg.maxOpenPositions) {
    return { ok: false, status: "MAX_OPEN_POSITIONS", reason: `Maximum open positions reached: ${cfg.maxOpenPositions}.` };
  }

  const freeUSDT = Number((account.balances || []).find(b => b.asset === "USDT")?.free || 0);
  const availableUSDT = Math.max(0, freeUSDT - cfg.reserveUSDT);

  const entry = Number(fresh.paperPlan.entry);
  const stopRaw = Number(fresh.paperPlan.stop);
  const targetRaw = Number(fresh.paperPlan.target1);
  const riskPct = entry > stopRaw ? (entry - stopRaw) / entry : 1;
  const riskSizedUSDT = riskPct > 0 ? cfg.maxRiskUSDT / riskPct : cfg.tradeUSDT;
  let quoteToSpend = Math.min(CFG.maxPositionUSDT, cfg.tradeUSDT, riskSizedUSDT, availableUSDT);
  quoteToSpend = Math.floor(quoteToSpend * 100) / 100;

  const notionalFilter = symbolInfo.filters?.find(f => f.filterType === "NOTIONAL") ||
    symbolInfo.filters?.find(f => f.filterType === "MIN_NOTIONAL");
  const minNotional = Number(notionalFilter?.minNotional || 5);
  const lot = symbolInfo.filters?.find(f => f.filterType === "LOT_SIZE");
  const stepSize = Number(lot?.stepSize || 0.00000001);
  const priceFilter = symbolInfo.filters?.find(f => f.filterType === "PRICE_FILTER");
  const tickSize = Number(priceFilter?.tickSize || 0.00000001);
  const ask = Number(book.askPrice || entry);
  const plannedStop = Number(floorToStep(Math.min(stopRaw, ask * 0.998), tickSize));
  const plannedTakeProfit = Number(ceilToStep(Math.max(targetRaw, ask * 1.002), tickSize));
  const grossQty = ask > 0 ? quoteToSpend / ask : 0;
  const protectedQty = Number(floorToStep(grossQty * (1 - CFG.feeRate), stepSize));
  const stopDistancePct = ask > plannedStop ? ((ask - plannedStop) / ask) * 100 : 999;
  const estimatedEntryFee = quoteToSpend * CFG.feeRate;
  const estimatedStopFee = protectedQty * plannedStop * CFG.feeRate;
  const estimatedTargetFee = protectedQty * plannedTakeProfit * CFG.feeRate;
  const estimatedRisk = protectedQty * (ask - plannedStop) + estimatedEntryFee + estimatedStopFee;
  const estimatedReward = protectedQty * (plannedTakeProfit - ask) - estimatedEntryFee - estimatedTargetFee;
  const estimatedNetRR = estimatedRisk > 0 ? estimatedReward / estimatedRisk : 0;

  if (quoteToSpend > CFG.maxPositionUSDT || quoteToSpend < minNotional) {
    return {
      ok: false,
      status: "POSITION_SIZE_REJECTED",
      reason: `Safe order size ${quoteToSpend} USDT must be between Binance minimum ${minNotional} and the hard ${CFG.maxPositionUSDT} USDT cap.`,
      requiredUSDT: minNotional,
      maxAllowedUSDT: CFG.maxPositionUSDT,
    };
  }
  if (!(plannedStop > 0 && plannedStop < ask && plannedTakeProfit > ask) || stopDistancePct > 5) {
    return { ok: false, status: "INVALID_PROTECTION_LEVELS", reason: "Stop/target levels are invalid or the stop exceeds 5%." };
  }
  if (protectedQty * plannedStop < minNotional || protectedQty * plannedTakeProfit < minNotional) {
    return {
      ok: false,
      status: "PROTECTION_BELOW_MIN_NOTIONAL",
      reason: `Trade rejected before buying: the protected ${quoteToSpend} USDT position would fall below Binance's ${minNotional} USDT minimum on an OCO leg.`,
      orderPlaced: false,
      maxAllowedUSDT: CFG.maxPositionUSDT,
    };
  }
  if (estimatedRisk > cfg.maxRiskUSDT) {
    return { ok: false, status: "RISK_LIMIT", reason: `Estimated risk ${round(estimatedRisk, 4)} USDT exceeds the 0.50 USDT cap.`, orderPlaced: false };
  }
  if (estimatedNetRR < cfg.minNetRewardRisk) {
    return {
      ok: false,
      status: "NET_RR_TOO_LOW",
      reason: `Net reward/risk ${round(estimatedNetRR, 2)} is below the required ${cfg.minNetRewardRisk.toFixed(2)} after fees and exchange rounding.`,
      orderPlaced: false,
    };
  }

  const token = crypto.randomUUID().replaceAll("-", "").slice(0, 8);
  const buyParams = {
    symbol,
    side: "BUY",
    type: "MARKET",
    quoteOrderQty: quoteToSpend.toFixed(2),
    newClientOrderId: `TSTB${token}`,
    newOrderRespType: "FULL",
  };

  const buy = await signedBinance("POST", "/api/v3/order", buyParams);
  const executedQty = Number(buy.executedQty || 0);
  const quoteSpent = Number(buy.cummulativeQuoteQty || quoteToSpend);
  const avgFill = executedQty > 0 ? quoteSpent / executedQty : entry;
  if (!(executedQty > 0)) {
    return { ok: false, status: "NO_FILL", reason: "Buy order returned no executed quantity.", orderId: buy.orderId };
  }

  const baseAsset = symbolInfo.baseAsset;
  const baseCommission = (buy.fills || [])
    .filter(f => f.commissionAsset === baseAsset)
    .reduce((sum, f) => sum + Number(f.commission || 0), 0);
  const sellQty = floorToStep(Math.max(0, executedQty - baseCommission), stepSize);
  const currentBook = await binance(`/api/v3/ticker/bookTicker?symbol=${symbol}`);
  const currentBid = Number(currentBook.bidPrice || avgFill);
  const takeProfit = ceilToStep(Math.max(targetRaw, currentBid * 1.002), tickSize);
  const stop = floorToStep(Math.min(stopRaw, currentBid * 0.998), tickSize);

  if (!(Number(sellQty) > 0 && Number(takeProfit) > currentBid && Number(stop) > 0 && Number(stop) < currentBid)) {
    const close = await emergencyClose(symbol, sellQty, token).catch(() => null);
    return {
      ok: Boolean(close),
      status: close ? "BOUGHT_THEN_SAFETY_CLOSED" : "UNPROTECTED_POSITION",
      reason: close ? "Protection values were invalid, so the position was closed immediately for safety." : "Bought but failed to protect or close the position.",
      orderId: buy.orderId,
      quoteSpentUSDT: round(quoteSpent, 2),
      quantity: sellQty,
      avgFillPrice: fmt(avgFill),
      protection: close ? "CLOSED_FAILSAFE" : "NONE",
      openPositionsAfter: openSymbols.length,
      maxOpenPositions: cfg.maxOpenPositions,
      riskUSDT: round(quoteSpent * riskPct, 4),
    };
  }

  let protection = "OCO";
  try {
    await signedBinance("POST", "/api/v3/orderList/oco", {
      symbol,
      side: "SELL",
      quantity: sellQty,
      listClientOrderId: `TSTL${token}`,
      aboveType: "LIMIT_MAKER",
      aboveClientOrderId: `TSTT${token}`,
      abovePrice: takeProfit,
      belowType: "STOP_LOSS",
      belowClientOrderId: `TSTS${token}`,
      belowStopPrice: stop,
    });
  } catch (ocoError) {
    try {
      await signedBinance("POST", "/api/v3/order", {
        symbol,
        side: "SELL",
        type: "STOP_LOSS",
        quantity: sellQty,
        stopPrice: stop,
        newClientOrderId: `TSTS${token}`,
      });
      protection = "STOP_ONLY";
    } catch (stopError) {
      const closed = await emergencyClose(symbol, sellQty, token).catch(() => null);
      if (!closed) {
        return {
          ok: false,
          status: "UNPROTECTED_POSITION",
          reason: `Bought, but protection and emergency close failed. OCO: ${ocoError.message}; Stop: ${stopError.message}`,
          orderId: buy.orderId,
          quoteSpentUSDT: round(quoteSpent, 2),
          quantity: sellQty,
          avgFillPrice: fmt(avgFill),
          protection: "NONE",
        };
      }
      protection = "CLOSED_FAILSAFE";
    }
  }

  return {
    ok: true,
    status: protection === "CLOSED_FAILSAFE" ? "BOUGHT_THEN_SAFETY_CLOSED" : "LIVE_SPOT_OPENED",
    symbol,
    orderId: buy.orderId,
    quoteSpentUSDT: round(quoteSpent, 2),
    quantity: sellQty,
    avgFillPrice: fmt(avgFill),
    stop,
    takeProfit,
    protection,
    openPositionsAfter: protection === "CLOSED_FAILSAFE" ? openSymbols.length : openSymbols.length + 1,
    maxOpenPositions: cfg.maxOpenPositions,
    riskUSDT: round(quoteSpent * riskPct + quoteSpent * CFG.feeRate * 2, 4),
  };
}

async function emergencyClose(symbol, quantity, token) {
  if (!(Number(quantity) > 0)) return null;
  return signedBinance("POST", "/api/v3/order", {
    symbol,
    side: "SELL",
    type: "MARKET",
    quantity,
    newClientOrderId: `TSTX${token}`,
    newOrderRespType: "FULL",
  });
}

function hasBinanceSigningKey() {
  return Boolean(process.env.BINANCE_API_KEY &&
    (process.env.BINANCE_API_PRIVATE_KEY || process.env.BINANCE_API_SECRET));
}

async function signedBinance(method, path, params = {}) {
  const apiKey = process.env.BINANCE_API_KEY;
  const privateKeyPem = process.env.BINANCE_API_PRIVATE_KEY;
  const secret = process.env.BINANCE_API_SECRET;
  if (!apiKey || (!privateKeyPem && !secret)) throw new Error("Binance API signing key is missing");

  const all = { ...params, recvWindow: 5000, timestamp: Date.now() };
  const query = Object.entries(all)
    .filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
  const signature = privateKeyPem
    ? await ed25519Base64(privateKeyPem, query)
    : await hmacHex(secret, query);
  const r = await fetch(`https://api.binance.com${path}?${query}&signature=${encodeURIComponent(signature)}`, {
    method,
    headers: { "X-MBX-APIKEY": apiKey, Accept: "application/json" },
    signal: AbortSignal.timeout(15_000),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${data.code || r.status}: ${data.msg || "Binance request failed"}`);
  return data;
}

async function ed25519Base64(privateKeyPem, text) {
  const normalized = String(privateKeyPem).replace(/\\n/g, "\n").trim();
  const base64 = normalized
    .replace("-----BEGIN PRIVATE KEY-----", "")
    .replace("-----END PRIVATE KEY-----", "")
    .replace(/\s+/g, "");
  const der = Uint8Array.from(Buffer.from(base64, "base64"));
  const key = await crypto.subtle.importKey(
    "pkcs8",
    der,
    { name: "Ed25519" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(text));
  return Buffer.from(signature).toString("base64");
}

async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function stepDecimals(step) {
  const s = String(step);
  if (s.includes("e-")) return Number(s.split("e-")[1]);
  const dot = s.indexOf(".");
  return dot < 0 ? 0 : s.length - dot - 1;
}

function floorToStep(value, step) {
  if (!(step > 0)) return String(value);
  const d = Math.min(stepDecimals(step), 12);
  const n = Math.floor((Number(value) + step * 1e-9) / step) * step;
  return n.toFixed(d).replace(/\.?0+$/, "");
}

function ceilToStep(value, step) {
  if (!(step > 0)) return String(value);
  const d = Math.min(stepDecimals(step), 12);
  const n = Math.ceil((Number(value) - step * 1e-9) / step) * step;
  return n.toFixed(d).replace(/\.?0+$/, "");
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
  const allCandles = rawKlines.map(k => ({
    openTime: Number(k[0]), open: Number(k[1]), high: Number(k[2]), low: Number(k[3]), close: Number(k[4]), volume: Number(k[5]), closeTime: Number(k[6]),
  }));
  const forming = allCandles.at(-1)?.closeTime >= Date.now() ? allCandles.at(-1) : null;
  const candles = allCandles.filter(c => c.closeTime < Date.now());
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
  // The breakout candle and retest candle are excluded from the resistance base.
  const prior20 = candles.slice(-22, -2);
  const resistance20 = Math.max(...prior20.map(c => c.high));
  const support20 = Math.min(...candles.slice(-21, -1).map(c => c.low));
  const bid = Number(book.bidPrice);
  const ask = Number(book.askPrice);
  const mid = (bid + ask) / 2;
  const spreadPct = mid > 0 ? ((ask - bid) / mid) * 100 : 999;
  const change24hPct = Number(ticker.priceChangePercent || 0);

  const trendUp = last.close > ema20 && ema20 > ema50;
  const trendDown = last.close < ema20 && ema20 < ema50;
  const preBreakout = candles.at(-3);
  const breakout = previous.close > resistance20 && preBreakout.close <= resistance20;
  const successfulRetest = breakout &&
    last.low <= resistance20 * 1.0025 &&
    last.close >= resistance20 &&
    last.close <= previous.close * 1.015;
  const nearBreakout = last.close >= resistance20 * 0.997;
  const healthyMomentum = rsi14 >= 54 && rsi14 <= 64;
  const breakoutVolumeRatio = avgVol20 > 0 ? previous.volume / avgVol20 : 0;
  const volumeConfirm = breakoutVolumeRatio >= 1.5;
  const liquid = Number(ticker.quoteVolume || 0) >= CFG.minQuoteVolume24h;
  const spreadOk = spreadPct <= CFG.maxSpreadPct;
  const notChasing = change24hPct <= 15;

  let decision = "WAIT";
  let reason = "No confirmed entry";
  if (trendUp && breakout && successfulRetest && healthyMomentum && volumeConfirm && liquid && spreadOk && notChasing) {
    decision = "BUY";
    reason = "Completed 15m breakout followed by a separate successful retest, with trend, volume and liquidity confirmation";
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
  // Gross targets are wider so the first target still clears 2:1 after Spot fees.
  const target1 = entry + 2.75 * stopDistance;
  const target2 = entry + 4 * stopDistance;
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

  const formingClose = Number(forming?.close || last.close);
  const formingRsi14 = rsi([...closes, formingClose], 14);
  const formingDuration = forming ? Math.max(1, forming.closeTime - forming.openTime) : 1;
  const formingProgress = forming ? Math.min(1, Math.max(0.1, (Date.now() - forming.openTime) / formingDuration)) : 1;
  const projectedVolumeRatio20 = forming && avgVol20 > 0 ? (forming.volume / formingProgress) / avgVol20 : 0;
  const minutesToClose = forming ? Math.max(1, Math.ceil((forming.closeTime - Date.now()) / 60_000)) : 0;
  const formingNearBreakout = formingClose >= resistance20 * 0.997 && formingClose <= resistance20 * 1.01;
  const earlyPotential = decision !== "BUY" && Boolean(forming) && minutesToClose <= 15 &&
    trendUp && formingNearBreakout && formingRsi14 >= 50 && formingRsi14 <= 72 &&
    projectedVolumeRatio20 >= 0.8 && liquid && spreadOk && notChasing;
  const earlyScore = Math.max(0, Math.min(100,
    (trendUp ? 25 : 0) +
    (formingNearBreakout ? 25 : 0) +
    (formingRsi14 >= 50 && formingRsi14 <= 72 ? 15 : 0) +
    (projectedVolumeRatio20 >= 0.8 ? 15 : 0) +
    (liquid ? 10 : 0) +
    (spreadOk ? 10 : 0)
  ));

  return {
    ok: true,
    symbol,
    decision,
    score,
    reason,
    price: last.close,
    market: { change24hPct: round(change24hPct, 2), quoteVolume24hUSDT: round(Number(ticker.quoteVolume || 0), 0), spreadPct: round(spreadPct, 4) },
    indicators: { ema20: round(ema20, 8), ema50: round(ema50, 8), rsi14: round(rsi14, 2), atr14: round(atr, 8), volumeRatio20: round(volumeRatio, 2), resistance20: round(resistance20, 8), support20: round(support20, 8) },
    checks: { trendUp, breakout, successfulRetest, nearBreakout, healthyMomentum, volumeConfirm, liquid, spreadOk, notChasing },
    earlyWatch: {
      potential: earlyPotential,
      minutesToClose,
      score: earlyScore,
      projectedEntry: round(ask || formingClose, 8),
      formingRsi14: round(formingRsi14, 2),
      projectedVolumeRatio20: round(projectedVolumeRatio20, 2),
      resistance20: round(resistance20, 8),
    },
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
