const API_BASES = [
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://data-api.binance.vision",
];

const CFG = {
  validationMode: false,
  capital: 20.08,
  maxPosition: 7,
  maxRisk: 0.20,
  dailyLossCap: 0.5,
  fee: 0.001,
  minVolume24h: 20_000_000,
  minDepthEachSide: 15_000,
  maxSpreadPct: 0.10,
  maxRise24hPct: 8,
  maxStopPct: 3,
  minNetRR: 3.0,
  minScore: 90,
  minAgeDays: 90,
  minRelativeVolume: 1.50,
  minTakerBuyRatio: 0.56,
  minBidAskDepthRatio: 1.20,
  maxLargestAskShare: 0.35,
  scanCount: 7,
  priorityCount: 2,
  maxOpenPaperPositions: 1,
  maxLiveTradesPerDay: 6,
  maxHoldHours: 48,
  duplicateHours: 6,
  approvalSeconds: 90,
  paperPositionHours: 72,
  publicBaseUrl: "https://tst-spot-signal.nurhanne-tarek13.workers.dev",
  vercelScanUrl: "https://tst-spot-signal.vercel.app/scan",
  defaultExecutorUrl: "https://tst-spot-signal.vercel.app",
  sourceChannels: {
    binance: "binance_announcements",
    cryptoQuant: "cryptoquant_alert",
    whaleAlert: "whale_alert_io",
  },
  telegramLookbackMessages: 15,
};

const EXCLUDED_BASES = new Set([
  "USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "TRY", "BRL",
  "BIDR", "IDRT", "UAH", "NGN", "RUB", "GBP", "AUD", "BUSD",
]);

const SAFE_B_SUFFIX_CRYPTO = new Set(["BNB", "ARB", "KUB", "WBB"]);
const PRODUCT_METADATA_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/execute") {
      return handleMakeExecute(request, env);
    }
    if (url.pathname === "/paper-status") {
      return paperStatus(env);
    }
    if (url.pathname === "/portfolio-status") {
      return portfolioStatus(env);
    }
    if (url.pathname === "/scanner-status") {
      const live = liveConfig(env);
      return output({
        ok: true,
        mode: live.enabled ? (live.autoExecute ? "LIVE_AUTO" : "CONFIRM_BEFORE_BUY") : "SIGNAL_ONLY",
        liveTrading: live.enabled,
        autoExecute: live.autoExecute,
        universe: "ALL_BINANCE_SPOT_USDT",
        cadence: "EVERY_MINUTE",
        deepScanPerRun: CFG.scanCount,
        minimumScore: CFG.minScore,
        risk: {
          maxPositionUSDT: CFG.maxPosition,
          maxRiskUSDT: CFG.maxRisk,
          dailyLossCapUSDT: CFG.dailyLossCap,
          maxOpenPositions: CFG.maxOpenPaperPositions,
          maxTradesPerDay: CFG.maxLiveTradesPerDay,
        },
      });
    }
    if (url.pathname === "/scan-preview") {
      return output(await scan(env, { sendAlerts: false }));
    }
    if (url.searchParams.get("test") === "preflight") {
      return livePreflight(env);
    }
    if (url.pathname === "/telegram-webhook") {
      return telegramWebhook(request, env);
    }
    if (url.searchParams.get("setup") === "telegram-webhook") {
      return setupTelegramWebhook(request, env);
    }
    if (url.searchParams.get("relay") === "telegram") {
      return relayTelegram(request, env);
    }
    if (url.searchParams.get("test") === "telegram") {
      const entry = 79000;
      const amount = 5 / entry;
      const text = [
        "🧪 TEST فقط — متشتريش BTC من الرسالة دي",
        "",
        "🟢 BUY NOW — BTC/USDT — SPOT",
        "",
        "━━━ اكتبي في Binance بالظبط ━━━",
        "",
        "1️⃣ PRICE / السعر",
        fmt(entry),
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
        `🛑 STOP LOSS: ${fmt(78210)}`,
        `🎯 TAKE PROFIT 1: ${fmt(80580)}`,
        `🎯 TAKE PROFIT 2: ${fmt(81370)}`,
        "",
        "━━━ شرط الدخول ━━━",
        "السعر الحالي لازم يكون بين:",
        `${fmt(78842)} → ${fmt(79158)}`,
        "",
        "👇 الزر تحت يفتح BTC/USDT Spot مباشرة.",
        "❌ TEST فقط — دي مش إشارة شراء حقيقية.",
      ].join("\n");
      await telegram(env, text, {
        inline_keyboard: [[
          { text: "🚀 افتحي BTC/USDT على Binance Spot", url: "https://www.binance.com/en/trade/BTC_USDT?type=spot" },
        ]],
      });
      return output({ ok: true, telegramTest: "sent", mode: "SIGNAL_ONLY", liveTrading: false });
    }
    if (url.searchParams.get("test") === "sources") {
      const sources = await getRiskSources();
      return output({ ok: true, sources: Object.fromEntries(Object.entries(sources).map(([name, messages]) =>
        [name, { messagesRead: messages.length, messages: messages.slice(0, 3) }])), liveTrading: false });
    }
    return output(await scan(env));
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      await monitorLivePositions(env);
      await monitorPaperPositions(env);
      await scan(env);
    })());
  },
};


async function handleMakeExecute(request, env) {
  try {
    if (request.method !== "POST") {
      return output({ ok: false, status: "METHOD_NOT_ALLOWED", reason: "POST required", liveTrading: false, orderPlaced: false }, 405);
    }
    const raw = await request.text();
    if (raw.length > 12_000) {
      return output({ ok: false, status: "PAYLOAD_TOO_LARGE", liveTrading: false, orderPlaced: false }, 413);
    }
    const body = JSON.parse(raw);
    // Demo-only endpoint: deliberately performs no Binance action.
    // Authentication becomes mandatory before live trading can ever be enabled.
    const signalId = String(body.signal_id || "").trim();
    const action = String(body.action || "").toUpperCase();
    const symbol = String(body.symbol || "").toUpperCase();
    const quoteAmount = Number(body.quote_amount_usdt);
    const stopLoss = Number(body.stop_loss_price);
    const takeProfit = Number(body.take_profit_price);
    const rawTimestamp = Number(body.timestamp);
    const timestampMs = rawTimestamp < 10_000_000_000 ? rawTimestamp * 1000 : rawTimestamp;

    if (!/^[A-Za-z0-9:_-]{6,128}$/.test(signalId)) {
      return output({ ok: false, status: "INVALID_SIGNAL_ID", liveTrading: false, orderPlaced: false }, 400);
    }
    if (!["BUY", "SELL"].includes(action)) {
      return output({ ok: false, status: "INVALID_ACTION", reason: "BUY or SELL only", liveTrading: false, orderPlaced: false }, 400);
    }
    if (!/^[A-Z0-9]{2,20}USDT$/.test(symbol)) {
      return output({ ok: false, status: "SYMBOL_NOT_ALLOWED", reason: "A Binance Spot USDT symbol is required", liveTrading: false, orderPlaced: false }, 400);
    }
    if (!Number.isFinite(quoteAmount) || quoteAmount <= 0 || quoteAmount > CFG.maxPosition) {
      return output({ ok: false, status: "QUOTE_LIMIT", reason: `quote_amount_usdt must be above 0 and at most ${CFG.maxPosition}`, liveTrading: false, orderPlaced: false }, 400);
    }
    if (!Number.isFinite(timestampMs) || Math.abs(Date.now() - timestampMs) > 120_000) {
      return output({ ok: false, status: "STALE_SIGNAL", reason: "Signal must be newer than 120 seconds", liveTrading: false, orderPlaced: false }, 400);
    }
    if (action === "BUY" && (!Number.isFinite(stopLoss) || !Number.isFinite(takeProfit) || stopLoss <= 0 || takeProfit <= stopLoss)) {
      return output({ ok: false, status: "INVALID_PROTECTION", reason: "Valid stop_loss_price and take_profit_price are required", liveTrading: false, orderPlaced: false }, 400);
    }

    const daily = await getDailyPaper(env);
    if (Number(daily.realizedPnlUSDT || 0) <= -CFG.dailyLossCap) {
      return output({ ok: false, status: "DAILY_LOSS_CAP", reason: "Daily loss cap reached", dailyLossCapUSDT: CFG.dailyLossCap, liveTrading: false, orderPlaced: false }, 429);
    }

    const dedupeKey = `make:signal:${signalId}`;
    if (await getState(env, dedupeKey)) {
      return output({ ok: false, status: "DUPLICATE_SIGNAL", signal_id: signalId, liveTrading: false, orderPlaced: false }, 409);
    }
    await putState(env, dedupeKey, { signalId, action, symbol, receivedAt: Date.now() }, 24 * 3600);

    return output({
      ok: true,
      status: "DEMO_ACCEPTED",
      mode: "PAPER_ONLY",
      signal_id: signalId,
      action,
      symbol,
      quote_amount_usdt: quoteAmount,
      stop_loss_price: action === "BUY" ? stopLoss : null,
      take_profit_price: action === "BUY" ? takeProfit : null,
      dailyRealizedPnlUSDT: Number(daily.realizedPnlUSDT || 0),
      dailyLossCapUSDT: CFG.dailyLossCap,
      liveTrading: false,
      orderPlaced: false,
      message: "Signal validated. No Binance order was submitted."
    });
  } catch (error) {
    return output({ ok: false, status: "BAD_REQUEST", error: String(error?.message || error), liveTrading: false, orderPlaced: false }, 400);
  }
}


async function scanVercelAndAlert(env) {
  try {
    const response = await fetch(CFG.vercelScanUrl, {
      headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-cloudflare/5.0" },
      signal: AbortSignal.timeout(50_000),
    });
    if (!response.ok) throw new Error(`Vercel scan failed: ${response.status}`);
    const scan = await response.json();

    const x = (scan.buySignals || [])[0] || (scan.best?.decision === "BUY" ? scan.best : null);
    if (!x || !x.paperPlan) {
      const early = (scan.results || [])
        .filter(candidate => candidate?.earlyWatch?.potential)
        .sort((a, b) => Number(b.earlyWatch?.score || 0) - Number(a.earlyWatch?.score || 0))[0] || null;
      if (early) {
        const warning = await maybeSendEarlyCapitalAlert(env, early);
        return {
          ok: true,
          status: warning.status,
          scanned: scan.scanned || 0,
          deepScanned: scan.deepScanned || 0,
          early: { symbol: early.symbol, minutesToClose: early.earlyWatch?.minutesToClose },
        };
      }
      return { ok: true, status: "NO_BUY_SIGNAL", scanned: scan.scanned || 0, deepScanned: scan.deepScanned || 0 };
    }

    const p = x.paperPlan;
    const signalId = String(x.signalId || `${x.symbol}:${x.price}:${x.indicators?.resistance20 || "na"}`);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(signalId));
    const signalHash = [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2, "0")).join("").slice(0, 8);
    const cacheKey = new Request(`https://minute-signal-cache.local/${x.symbol}/${signalHash}`);
    if (await caches.default.match(cacheKey)) {
      return { ok: true, status: "DUPLICATE_SUPPRESSED", symbol: x.symbol };
    }

    const live = liveConfig(env);

    const entry = Number(p.entry);
    const entryLow = entry * 0.998;
    const entryHigh = entry * 1.002;
    const target2 = Number(p.target2 || (entry + 3 * (entry - Number(p.stop))));
    const baseAsset = x.symbol.endsWith("USDT") ? x.symbol.slice(0, -4) : x.symbol;
    const pair = x.symbol.endsWith("USDT") ? `${baseAsset}/USDT` : x.symbol;
    const openText = `up to ${live.maxOpenPositions}`;

    if (CFG.validationMode) {
      await telegram(env, [
        `🧪 PAPER فقط — ${pair}`,
        `💵 حجم الاختبار: حتى ${live.tradeUSDT} USDT`,
        `🛑 Stop: ${fmt(Number(p.stop))}`,
        `🎯 TP1: ${fmt(Number(p.target1))}`,
        `🎯 TP2: ${fmt(target2)}`,
        "",
        "لا يوجد زر BUY ولن يتم إرسال أي أمر إلى Binance.",
        "الفرصة تُسجل للتحقق من أداء الاستراتيجية فقط."
      ].join("\n"));
      await caches.default.put(cacheKey, new Response("paper-alert", {
        headers: { "Cache-Control": "max-age=3600" },
      }));
      return { ok: true, status: "PAPER_ALERT_SENT", symbol: x.symbol, signalId };
    }

    if (live.autoExecute && live.enabled) {
      const execution = await executeLiveSpotBuy(env, x.symbol, signalHash);
      if (execution.ok) {
        const stats = await recordAutoExecution(env, { ...execution, symbol: x.symbol });
        await telegram(env, [
          `🤖 AUTO BUY — ${pair} — SPOT`,
          `💵 المستخدم: ${execution.quoteSpentUSDT ?? "—"} USDT`,
          execution.avgFillPrice != null ? `💲 متوسط التنفيذ: ${execution.avgFillPrice}` : null,
          execution.stop != null ? `🛑 Stop: ${execution.stop}` : null,
          execution.takeProfit != null ? `🎯 Take Profit: ${execution.takeProfit}` : null,
          execution.openPositionsAfter != null ? `📂 الصفقات المفتوحة: ${execution.openPositionsAfter}/${execution.maxOpenPositions}` : null,
          execution.equityUSDT != null ? `💰 إجمالي الرصيد الحالي: ${execution.equityUSDT} USDT` : null,
          stats.lastKnownPnlUSDT != null ? `📈 التغير من بداية التتبع: ${stats.lastKnownPnlUSDT >= 0 ? "+" : ""}${stats.lastKnownPnlUSDT} USDT` : null,
          "",
          "الحجم بيتعاد حسابه من الرصيد والمخاطرة قبل كل صفقة؛ الربح لا يعني إن الصفقة التالية مضمونة."
        ].filter(Boolean).join("\n"));
        await caches.default.put(cacheKey, new Response("executed", {
          headers: { "Cache-Control": "max-age=3600" },
        }));
        return { ok: true, status: "AUTO_EXECUTED", symbol: x.symbol, signalId, execution };
      }

      if (["MAX_OPEN_POSITIONS","INSUFFICIENT_BALANCE","BELOW_MIN_NOTIONAL","CAPITAL_CONSTRAINED"].includes(execution.status)) {
        await sendCapitalConstraintAlert(env, x.symbol, execution);
      }
      await caches.default.put(cacheKey, new Response("auto-rejected", {
        headers: { "Cache-Control": "max-age=900" },
      }));
      return { ok: true, status: "AUTO_NOT_EXECUTED", symbol: x.symbol, reason: execution.reason || execution.status };
    }

    const message = [
      `🟢 فرصة SPOT — ${pair}`,
      "",
      live.compoundEnabled
        ? `💵 الحجم: ديناميكي — ${live.tradeEquityPct}% من الرصيد، بحد أقصى ${live.maxTradeUSDT} USDT وداخل حد المخاطرة`
        : `💵 حجم الصفقة المستهدف: حتى ${live.tradeUSDT} USDT`,
      `📂 الصفقات المفتوحة: ${openText}`,
      `🛑 Stop: ${fmt(Number(p.stop))}`,
      `🎯 TP1: ${fmt(Number(p.target1))}`,
      `🎯 TP2: ${fmt(target2)}`,
      "",
      `✅ السعر صالح تقريبًا بين ${fmt(entryLow)} و ${fmt(entryHigh)}`,
      "",
      "👇 دوسي BUY فقط — البوت يعيد الفحص قبل أي تنفيذ.",
      "",
      "❌ Spot فقط — لا Futures / Perp / Short.",
      `⏱️ الزر صالح ${live.approvalSeconds} ثانية فقط.`,
    ].join("\n");

    await ensureTelegramWebhook(env);
    await telegram(env, message, {
      inline_keyboard: [[
        { text: `✅ BUY`, callback_data: `live_buy:${x.symbol}:${signalHash}` },
        { text: "❌ تجاهل", callback_data: `live_no:${x.symbol}:${signalHash}` },
      ]],
    });

    await caches.default.put(cacheKey, new Response("sent", {
      headers: { "Cache-Control": "max-age=3600" },
    }));
    return {
      ok: true,
      status: "ALERT_SENT",
      symbol: x.symbol,
      signalId,
      portfolio: { maxOpenPositions: live.maxOpenPositions },
    };
  } catch (error) {
    return { ok: false, status: "SCHEDULER_ERROR", error: String(error?.message || error) };
  }
}


async function maybeSendEarlyCapitalAlert(env, candidate) {
  const cfg = liveConfig(env);
  const symbol = String(candidate.symbol || "");
  const early = candidate.earlyWatch || {};
  const signalId = String(candidate.signalId || `${symbol}:${early.resistance20 || candidate.price || "na"}`);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(signalId));
  const signalHash = [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2, "0")).join("").slice(0, 8);
  const cacheKey = new Request(`https://early-capital-alert.local/${symbol}/${signalHash}`);
  if (await caches.default.match(cacheKey)) return { status: "EARLY_WARNING_DUPLICATE" };

  const account = await fetchExecutorAccountStatus(env);
  if (!account.ok) return { status: "EARLY_ACCOUNT_CHECK_FAILED", reason: account.reason || account.status };

  const capUSDT = cfg.capitalCapEGP / cfg.egpPerUSDT;
  const equityUSDT = Math.max(0, Number(account.equityUSDT || 0));
  const targetEquityUSDT = Math.min(
    capUSDT,
    cfg.maxTradeUSDT / Math.max(0.05, cfg.tradeEquityPct / 100)
  );
  const suggestedAddUSDT = Math.max(0, Math.min(capUSDT - equityUSDT, targetEquityUSDT - equityUSDT));
  const suggestedAddEGP = Math.floor((suggestedAddUSDT * cfg.egpPerUSDT) / 50) * 50;

  if (suggestedAddEGP < 100) {
    await caches.default.put(cacheKey, new Response("capital-sufficient", {
      headers: { "Cache-Control": "max-age=3600" },
    }));
    return { status: "EARLY_CAPITAL_ALREADY_SUFFICIENT" };
  }

  const pair = symbol.endsWith("USDT") ? `${symbol.slice(0, -4)}/USDT` : symbol;
  await telegram(env, [
    `👀 تنبيه مبكر — ${pair}`,
    `الفرصة قريبة من شروط الدخول وقد تتأكد خلال نحو ${Math.min(cfg.earlyWarningMinutes, Number(early.minutesToClose || cfg.earlyWarningMinutes))} دقيقة عند إغلاق الشمعة.`,
    "",
    `💰 إجمالي رصيد Spot التقريبي: ${round2(equityUSDT)} USDT`,
    `💵 لو حابة تجهزي الحجم الأقصى المحدد للصفقة: حطي تقريبًا ${suggestedAddEGP} جنيه فقط.`,
    `🧱 الحد الصارم لإجمالي رأس المال: ${cfg.capitalCapEGP} جنيه (حوالي ${round2(capUSDT)} USDT بسعر مرجعي ${cfg.egpPerUSDT} جنيه/USDT).`,
    "",
    "⚠️ دي مراقبة مبكرة وليست إشارة BUY مؤكدة. لا تشتري يدويًا؛ استني رسالة BUY النهائية وإعادة الفحص.",
    "لن يتم أي إيداع أو شراء تلقائي من هذا التنبيه."
  ].join("\n"));

  await caches.default.put(cacheKey, new Response("sent", {
    headers: { "Cache-Control": "max-age=3600" },
  }));
  return { status: "EARLY_CAPITAL_ALERT_SENT", symbol, suggestedAddEGP };
}

async function fetchExecutorAccountStatus(env) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    return { ok: false, status: "RELAY_SECRET_MISSING", reason: "Telegram relay secret is missing." };
  }
  const executorBase = String(env.EXECUTOR_URL || CFG.defaultExecutorUrl).replace(/\/$/, "");
  const endpoint = `${executorBase}/executor/account-status`;
  const body = JSON.stringify({ purpose: "EARLY_CAPITAL_CHECK", timestamp: Date.now() });
  const ts = String(Date.now());
  const signature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Executor-Timestamp": ts,
        "X-Executor-Signature": signature,
        "User-Agent": "tst-spot-signal-cloudflare-capital/1.0",
      },
      body,
      signal: AbortSignal.timeout(15_000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      return { ok: false, status: data.status || "ACCOUNT_CHECK_REJECTED", reason: data.reason || data.error || `Executor returned ${response.status}` };
    }
    return data;
  } catch (error) {
    return { ok: false, status: "ACCOUNT_CHECK_UNREACHABLE", reason: String(error?.message || error) };
  }
}

async function scan(env, options = {}) {
  try {
    const sendAlerts = options.sendAlerts !== false;
    const paperDaily = await getDailyPaper(env);
    if (Number(paperDaily.realizedPnlUSDT || 0) <= -CFG.dailyLossCap) {
      return resultBase({
        status: "DAILY_LOSS_CAP",
        dailyRealizedPnlUSDT: Number(paperDaily.realizedPnlUSDT || 0),
      });
    }

    const [tickers, books, exchangeInfo, products, btc1h, btc4h, riskSources] = await Promise.all([
      binance("/api/v3/ticker/24hr"),
      binance("/api/v3/ticker/bookTicker"),
      binance("/api/v3/exchangeInfo"),
      getBinanceProductMetadata().catch(() => new Map()),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=100"),
      getRiskSources().catch(() => ({ binance: [], cryptoQuant: [], whaleAlert: [] })),
    ]);

    const regime = marketRegime(btc1h.map(candle), btc4h.map(candle));
    if (!regime.longAllowed) {
      return resultBase({ status: "MARKET_RISK_OFF", marketRegime: regime });
    }

    const tradable = new Map(
      exchangeInfo.symbols
        .filter(s => s.status === "TRADING" && s.quoteAsset === "USDT" && s.isSpotTradingAllowed && s.ocoAllowed)
        .map(s => [s.symbol, s])
    );
    const bookMap = new Map(books.map(b => [b.symbol, b]));
    const selection = await shortlist(env, tickers, tradable, bookMap, products);
    const candidates = selection.candidates;
    const analyses = [];

    for (let i = 0; i < candidates.length; i += 3) {
      const group = candidates.slice(i, i + 3);
      const groupResults = await Promise.all(
        group.map(x => analyzeSymbol(x, tradable.get(x.symbol), bookMap.get(x.symbol), regime, riskSources))
      );
      analyses.push(...groupResults);
    }

    const valid = analyses.filter(x => x.valid).sort((a, b) => b.score - a.score);
    const best = valid[0] || null;
    let alertSent = false;
    const activePaper = (await getState(env, "paper:active") || []).filter(Boolean);
    const activeLive = (await getState(env, "live:positions:active") || []).filter(Boolean);
    const live = liveConfig(env);
    let liveExecution = null;

    if (sendAlerts && best && live.enabled && live.autoExecute && activeLive.length < live.maxOpenPositions) {
      const daily = await getLiveDailyRisk(env);
      const pnl = await getLiveDailyPnl(env);
      const cache = caches.default;
      const cacheKey = new Request(`https://scanner-cache.local/${best.symbol}/${best.signalId}`);
      if (daily.trades < CFG.maxLiveTradesPerDay && pnl.netPnlUSDT > -live.dailyLossCapUSDT && !(await cache.match(cacheKey))) {
        await cache.put(cacheKey, new Response("executing", {
          headers: { "Cache-Control": `max-age=${CFG.duplicateHours * 3600}` },
        }));
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(best.signalId));
        const signalHash = [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2, "0")).join("").slice(0, 8);
        liveExecution = await executeLiveSpotBuy(env, best.symbol, signalHash);
        if (liveExecution.ok) {
          const stats = await recordAutoExecution(env, { ...liveExecution, symbol: best.symbol });
          await telegram(env, [
            `✅ تم شراء ${best.symbol.replace("USDT", "/USDT")} — SPOT`,
            `درجة الإشارة: ${best.score}/100`,
            `💵 المستخدم: ${liveExecution.quoteSpentUSDT} USDT`,
            `📦 الكمية: ${liveExecution.quantity}`,
            `💲 متوسط التنفيذ: ${liveExecution.avgFillPrice}`,
            `🛑 Stop: ${liveExecution.stop}`,
            `🎯 Take Profit: ${liveExecution.takeProfit}`,
            `📂 الصفقات المفتوحة: ${liveExecution.openPositionsAfter}/${liveExecution.maxOpenPositions}`,
            `📊 صفقات اليوم: ${daily.trades + 1}/${CFG.maxLiveTradesPerDay}`,
            liveExecution.protection === "OCO" ? "🛡️ الحماية مفعلة: Take Profit + Stop Loss (OCO)." : "🛡️ تم تطبيق مسار الحماية البديل.",
            stats.lastKnownPnlUSDT != null ? `📈 التغير من بداية التتبع: ${stats.lastKnownPnlUSDT >= 0 ? "+" : ""}${stats.lastKnownPnlUSDT} USDT` : null,
          ].filter(Boolean).join("\n"));
          alertSent = true;
        }
      }
    } else if (sendAlerts && best && live.enabled && !live.autoExecute && activeLive.length < live.maxOpenPositions) {
      const daily = await getLiveDailyRisk(env);
      const pnl = await getLiveDailyPnl(env);
      const cache = caches.default;
      const cacheKey = new Request(`https://scanner-cache.local/${best.symbol}/${best.signalId}`);
      if (daily.trades < CFG.maxLiveTradesPerDay && pnl.netPnlUSDT > -live.dailyLossCapUSDT && !(await cache.match(cacheKey))) {
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(best.signalId));
        const signalHash = [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2, "0")).join("").slice(0, 8);
        const plan = best.paperPlan;
        const pair = best.symbol.replace("USDT", "/USDT");
        await ensureTelegramWebhook(env);
        await telegram(env, [
          `🟢 فرصة SPOT — ${pair}`,
          `درجة التأكيد: ${best.score}/100`,
          `💵 الحد الأقصى: ${live.tradeUSDT} USDT`,
          `🛑 Stop: ${fmt(Number(plan.stop))}`,
          `🎯 TP1: ${fmt(Number(plan.target1))}`,
          `🎯 TP2: ${fmt(Number(plan.target2))}`,
          `📊 صفقات اليوم: ${daily.trades}/${CFG.maxLiveTradesPerDay}`,
          "",
          "👇 لن يتم الشراء إلا بعد ضغط BUY.",
          "عند الضغط سيُعاد فحص السعر والسيولة والاتجاه والحساب والحماية قبل التنفيذ.",
          `⏱️ الزر صالح ${live.approvalSeconds} ثانية فقط.`,
        ].join("\n"), {
          inline_keyboard: [[
            { text: "✅ BUY", callback_data: `live_buy:${best.symbol}:${signalHash}` },
            { text: "❌ تجاهل", callback_data: `live_no:${best.symbol}:${signalHash}` },
          ]],
        });
        await cache.put(cacheKey, new Response("approval-sent", {
          headers: { "Cache-Control": `max-age=${CFG.duplicateHours * 3600}` },
        }));
        alertSent = true;
      }
    } else if (sendAlerts && best && !live.enabled && activePaper.length < CFG.maxOpenPaperPositions) {
      const cache = caches.default;
      const cacheKey = new Request(`https://scanner-cache.local/${best.symbol}/${best.signalId}`);
      if (!(await cache.match(cacheKey))) {
        const approvalId = crypto.randomUUID().replaceAll("-", "").slice(0, 20);
        const pending = {
          id: approvalId,
          symbol: best.symbol,
          signalId: best.signalId,
          candidate: best,
          createdAt: Date.now(),
          expiresAt: Date.now() + CFG.approvalSeconds * 1000,
          used: false,
        };
        await putState(env, `pending:${approvalId}`, pending, CFG.approvalSeconds + 120);
        await telegram(env, `${alertText(best, regime)}\n\n⏱️ الموافقة الورقية صالحة ${CFG.approvalSeconds} ثانية فقط.`, {
          inline_keyboard: [[
            { text: "✅ تنفيذ ورقي", callback_data: `paper_yes:${approvalId}` },
            { text: "❌ رفض", callback_data: `paper_no:${approvalId}` },
          ]],
        });
        await cache.put(cacheKey, new Response("sent", {
          headers: { "Cache-Control": `max-age=${CFG.duplicateHours * 3600}` },
        }));
        alertSent = true;
      }
    }

    return resultBase({
      status: best
        ? liveExecution?.ok ? "LIVE_EXECUTED"
          : activeLive.length >= live.maxOpenPositions ? "POSITION_LIMIT_WAIT"
            : "VALID_SETUP_FOUND"
        : "NO_VALID_SETUP",
      liveTrading: live.enabled,
      autoExecute: live.autoExecute,
      marketRegime: regime,
      surfaceUniverseSize: selection.eligibleCount,
      rotationCursor: selection.cursor,
      candidatesScanned: candidates.map(x => x.symbol),
      deepScanned: analyses.length,
      activePaperPositions: activePaper.length,
      activeLivePositions: activeLive.length,
      alertSent,
      liveExecution,
      selected: best,
      results: analyses,
    });
  } catch (error) {
    return { ok: false, error: String(error?.message || error), liveTrading: false, orderPlaced: false };
  }
}

async function shortlist(env, tickers, tradable, bookMap, products) {
  const eligible = tickers
    .filter(t => tradable.has(t.symbol) && bookMap.has(t.symbol))
    .map(t => ({
      symbol: t.symbol,
      base: tradable.get(t.symbol).baseAsset,
      volume: Number(t.quoteVolume),
      change: Number(t.priceChangePercent),
      trades: Number(t.count || 0),
    }))
    .filter(x => isAllowedProduct(x, products.get(x.symbol)))
    .filter(x => x.volume >= CFG.minVolume24h)
    .filter(x => x.change >= -8 && x.change <= CFG.maxRise24hPct);

  const byVolume = [...eligible].sort((a, b) => b.volume - a.volume)[0];
  const byMomentum = [...eligible]
    .filter(x => x.change > 0)
    .sort((a, b) => (b.change * Math.log10(Math.max(b.volume, 1))) - (a.change * Math.log10(Math.max(a.volume, 1))))[0];
  const leaders = [...new Map([byVolume, byMomentum].filter(Boolean).map(x => [x.symbol, x])).values()]
    .slice(0, CFG.priorityCount);
  const leaderSymbols = new Set(leaders.map(x => x.symbol));
  const rotationPool = eligible.filter(x => !leaderSymbols.has(x.symbol)).sort((a, b) => a.symbol.localeCompare(b.symbol));
  const state = await getState(env, "scanner:rotation") || { cursor: 0 };
  const cursor = rotationPool.length ? Number(state.cursor || 0) % rotationPool.length : 0;
  const remaining = Math.max(0, CFG.scanCount - leaders.length);
  const rotated = [];
  for (let i = 0; i < Math.min(remaining, rotationPool.length); i++) {
    rotated.push(rotationPool[(cursor + i) % rotationPool.length]);
  }
  const nextCursor = rotationPool.length ? (cursor + rotated.length) % rotationPool.length : 0;
  await putState(env, "scanner:rotation", { cursor: nextCursor, updatedAt: Date.now() }, 7 * 24 * 3600);
  return { candidates: [...leaders, ...rotated], eligibleCount: eligible.length, cursor: nextCursor };
}

function isAllowedProduct(summary, product) {
  const base = String(summary.base || "").toUpperCase();
  if (!base || EXCLUDED_BASES.has(base)) return false;
  if (/(UP|DOWN|BULL|BEAR)$/.test(base)) return false;

  const tags = (product?.tags || []).map(x => String(x).toLowerCase());
  const name = String(product?.name || "").toLowerCase();
  if (product?.etf || tags.some(x => x.includes("bstock") || x.includes("leveraged"))) return false;
  if (/bstock|tokenized stock|leveraged token/.test(name)) return false;

  // If Binance's product tags are temporarily unavailable, fail closed on the
  // naming convention used by bStocks while preserving known crypto symbols.
  if (!product && base.endsWith("B") && !SAFE_B_SUFFIX_CRYPTO.has(base)) return false;
  return true;
}

async function analyzeSymbol(summary, symbolInfo, book, regime, riskSources = {}) {
  try {
    const [raw15, raw1h, raw4h, raw1d, depth] = await Promise.all([
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=15m&limit=120`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1h&limit=100`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=4h&limit=100`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1d&limit=${CFG.minAgeDays + 5}`),
      binance(`/api/v3/depth?symbol=${summary.symbol}&limit=100`),
    ]);
    const c15 = closed(raw15.map(candle));
    const c1h = closed(raw1h.map(candle));
    const c4h = closed(raw4h.map(candle));
    const c1d = closed(raw1d.map(candle));
    if (c15.length < 60 || c1h.length < 60 || c4h.length < 60) {
      return { symbol: summary.symbol, valid: false, status: "INSUFFICIENT_HISTORY" };
    }
    const bid = Number(book.bidPrice);
    const ask = Number(book.askPrice);
    const mid = (bid + ask) / 2;
    const spread = ((ask - bid) / mid) * 100;
    const depthStats = orderBookStats(depth, mid);
    const trend = symbolTrend(c1h, c4h);
    const breakout = summary.symbol === "TSTUSDT" ? tstBreakoutRetest(c15, ask) : breakoutRetest(c15);
    const pullback = summary.symbol !== "TSTUSDT" && trend.aligned ? pullbackBounce(c15) : null;
    const setup = breakout || pullback;
    const atr = atr14(c15);
    const entry = ask;
    const atrPct = entry > 0 ? (atr / entry) * 100 : 999;
    const last15 = c15.at(-1);
    const volumeMedian = median(c15.slice(-21, -1).map(x => x.quoteVolume));
    const relativeVolume = volumeMedian > 0 ? last15.quoteVolume / volumeMedian : 0;
    const flowWindow = c15.slice(-8);
    const flowQuote = flowWindow.reduce((sum, x) => sum + x.quoteVolume, 0);
    const takerBuyRatio = flowQuote > 0
      ? flowWindow.reduce((sum, x) => sum + Number(x.takerBuyQuote || 0), 0) / flowQuote
      : 0;
    const momentumRsi = rsi14(c15.map(x => x.close));
    const close15 = c15.map(x => x.close);
    const trend15m = close15.at(-1) > ema(close15, 20) && ema(close15, 20) > ema(close15, 50);
    const swingLow = setup?.retest?.low || setup?.trigger?.low || c15.at(-2).low;
    const stopRaw = summary.symbol === "TSTUSDT"
      ? 0.01794
      : Math.min(swingLow * 0.998, entry - 1.15 * atr);
    const stop = roundPrice(stopRaw, entry);
    const stopPct = entry > stop ? ((entry - stop) / entry) * 100 : 999;
    const riskUnit = entry - stop;

    const filters = symbolInfo.filters || [];
    const lot = filters.find(f => f.filterType === "LOT_SIZE");
    const notional = filters.find(f => f.filterType === "NOTIONAL" || f.filterType === "MIN_NOTIONAL");
    const step = Number(lot?.stepSize || "0.00000001");
    const minNotional = Number(notional?.minNotional || "5");
    const qtyBudget = CFG.maxPosition / entry;
    const qtyRisk = riskUnit > 0 ? CFG.maxRisk / riskUnit : 0;
    const quantity = floorStep(Math.min(qtyBudget, qtyRisk), step);
    const position = quantity * entry;
    const entryFee = position * CFG.fee;
    const stopFee = quantity * stop * CFG.fee;
    const riskFees = quantity * riskUnit + entryFee + stopFee;
    const target1 = roundPrice(entry + 2.75 * riskUnit, entry);
    const target2 = roundPrice(entry + 3.5 * riskUnit, entry);
    const exitFee1 = quantity * target1 * CFG.fee;
    const netReward1 = quantity * (target1 - entry) - entryFee - exitFee1;
    const netRR = riskFees > 0 ? netReward1 / riskFees : 0;

    const protectedStopNotional = quantity * stop;
    const protectedTargetNotional = quantity * target1;
    const scoreBreakdown = {
      marketRegime: regime.longAllowed ? 10 : 0,
      multiTimeframeTrend: trend.aligned ? 15 : 0,
      confirmedRetest: setup ? 20 : 0,
      relativeVolume: relativeVolume >= CFG.minRelativeVolume ? 10 : 0,
      takerBuyPressure: takerBuyRatio >= CFG.minTakerBuyRatio ? 10 : 0,
      orderBookSupport: depthStats.bidAskRatio >= CFG.minBidAskDepthRatio && depthStats.largestAskShare <= CFG.maxLargestAskShare ? 10 : 0,
      tightSpread: spread <= CFG.maxSpreadPct ? 5 : 0,
      deepLiquidity: depthStats.bid >= CFG.minDepthEachSide && depthStats.ask >= CFG.minDepthEachSide ? 5 : 0,
      healthyMomentum: momentumRsi >= 52 && momentumRsi <= 66 ? 5 : 0,
      notOverextended: summary.change <= CFG.maxRise24hPct && atrPct >= 0.15 && atrPct <= 3.5 ? 5 : 0,
      protectableOrder: protectedStopNotional >= minNotional * 1.02 && protectedTargetNotional >= minNotional * 1.02 ? 5 : 0,
    };
    const score = Object.values(scoreBreakdown).reduce((sum, value) => sum + value, 0);

    const checks = {
      btcMarketAllowsLongs: regime.longAllowed,
      cryptoProductOnly: true,
      ageAtLeast90Days: c1d.length >= CFG.minAgeDays,
      volume24hAbove20M: summary.volume >= CFG.minVolume24h,
      notOverextended24h: summary.change <= CFG.maxRise24hPct,
      trend15mAligned: trend15m,
      multiTimeframeTrendAligned: trend.aligned,
      confirmedSetup: Boolean(setup),
      relativeVolumeConfirmed: relativeVolume >= CFG.minRelativeVolume,
      takerBuyPressureConfirmed: takerBuyRatio >= CFG.minTakerBuyRatio,
      spreadBelow010Pct: spread <= CFG.maxSpreadPct,
      bidDepthSufficient: depthStats.bid >= CFG.minDepthEachSide,
      askDepthSufficient: depthStats.ask >= CFG.minDepthEachSide,
      bidDepthDominates: depthStats.bidAskRatio >= CFG.minBidAskDepthRatio,
      noSingleSellWall: depthStats.largestAskShare <= CFG.maxLargestAskShare,
      rsiHealthy: momentumRsi >= 52 && momentumRsi <= 66,
      atrHealthy: atrPct >= 0.15 && atrPct <= 3.5,
      stopBelowEntry: stop > 0 && stop < entry,
      stopWithin3Pct: stopPct > 0 && stopPct <= CFG.maxStopPct,
      minimumOrderMet: position + 0.000001 >= minNotional,
      ocoSupported: Boolean(symbolInfo.ocoAllowed),
      protectedLegsMeetNotional: protectedStopNotional >= minNotional * 1.02 && protectedTargetNotional >= minNotional * 1.02,
      positionAtMost7USDT: position <= CFG.maxPosition + 0.000001,
      riskAtMost020USDT: riskFees <= CFG.maxRisk,
      netRewardRiskAtLeast30: netRR >= CFG.minNetRR,
      scoreAtLeast90: score >= CFG.minScore,
    };

    const sourceRisk = assessSourceRisk(riskSources, summary.base, summary.symbol);
    checks.noOfficialBinanceRisk = !sourceRisk.binance.blocked;
    checks.noOnChainMarketRisk = !sourceRisk.cryptoQuant.blocked;
    checks.noLargeExchangeDepositRisk = !sourceRisk.whaleAlert.blocked;

    const hardCheckKeys = [
      "btcMarketAllowsLongs", "cryptoProductOnly", "ageAtLeast90Days", "volume24hAbove20M",
      "notOverextended24h", "trend15mAligned", "multiTimeframeTrendAligned", "confirmedSetup",
      "relativeVolumeConfirmed", "takerBuyPressureConfirmed", "spreadBelow010Pct",
      "bidDepthSufficient", "askDepthSufficient", "bidDepthDominates", "noSingleSellWall",
      "rsiHealthy", "atrHealthy", "stopBelowEntry", "stopWithin3Pct",
      "minimumOrderMet", "ocoSupported", "protectedLegsMeetNotional", "positionAtMost7USDT",
      "riskAtMost020USDT", "netRewardRiskAtLeast30", "scoreAtLeast90", "noOfficialBinanceRisk",
      "noOnChainMarketRisk", "noLargeExchangeDepositRisk",
    ];
    const hardChecksPassed = hardCheckKeys.every(key => checks[key]);
    const valid = hardChecksPassed && score >= CFG.minScore;

    return {
      symbol: summary.symbol,
      valid,
      status: valid ? "READY_SIGNAL" : "WAIT",
      setup: setup?.type || null,
      officialSources: sourceRisk,
      checks,
      scoreBreakdown,
      market: {
        bid: fmt(bid), ask: fmt(ask), spreadPct: round(spread, 4),
        change24hPct: round(summary.change, 3), volume24hUSDT: round(summary.volume, 2),
        bidDepth1PctUSDT: round(depthStats.bid, 2), askDepth1PctUSDT: round(depthStats.ask, 2),
        bidAskDepthRatio: round(depthStats.bidAskRatio, 3), largestAskShare: round(depthStats.largestAskShare, 3),
        takerBuyRatio: round(takerBuyRatio, 3), relativeVolume: round(relativeVolume, 2), rsi15m: round(momentumRsi, 2),
        atr15m: fmt(atr), atrPct: round(atrPct, 3), stopDistancePct: round(stopPct, 2), ageDaysObserved: c1d.length,
      },
      plan: valid ? {
        entry: fmt(entry), stop: fmt(stop), target1: fmt(target1), target2: fmt(target2),
        quantity: trim(quantity), positionUSDT: round(position, 4),
        riskIncludingFeesUSDT: round(riskFees, 4),
        feesEntryAndTarget1USDT: round(entryFee + exitFee1, 5),
        netRewardRisk: round(netRR, 2),
        invalidation: `إلغاء الفكرة تحت ${fmt(stop)} أو عند فقد مستوى إعادة الاختبار`,
      } : null,
      signalId: `${setup?.type || "none"}-${setup?.time || 0}-${round(entry, 8)}`,
      score,
    };
  } catch (error) {
    return { symbol: summary.symbol, valid: false, status: "DATA_ERROR", error: String(error?.message || error) };
  }
}

function marketRegime(h1, h4) {
  const a = closed(h1), b = closed(h4);
  const close1 = a.at(-1).close, close4 = b.at(-1).close;
  const e20_1 = ema(a.map(x => x.close), 20), e50_1 = ema(a.map(x => x.close), 50);
  const e20_4 = ema(b.map(x => x.close), 20), e50_4 = ema(b.map(x => x.close), 50);
  const longAllowed = close1 > e50_1 && close4 > e50_4 && e20_1 >= e50_1 * 0.995;
  return {
    longAllowed,
    state: longAllowed ? "LONGS_ALLOWED" : "RISK_OFF",
    btc1hClose: fmt(close1), btc4hClose: fmt(close4),
    btc1hEMA20: fmt(e20_1), btc1hEMA50: fmt(e50_1),
    btc4hEMA20: fmt(e20_4), btc4hEMA50: fmt(e50_4),
  };
}

function symbolTrend(h1, h4) {
  const closes1h = h1.map(x => x.close);
  const closes4h = h4.map(x => x.close);
  const e9_1h = ema(closes1h, 9), e20_1h = ema(closes1h, 20), e50_1h = ema(closes1h, 50);
  const e20_4h = ema(closes4h, 20), e50_4h = ema(closes4h, 50);
  const last1h = h1.at(-1), prev1h = h1.at(-2), last4h = h4.at(-1);
  const rising1h = e9_1h > e20_1h && e20_1h > e50_1h && last1h.close > e20_1h && last1h.close >= prev1h.close * 0.995;
  const rising4h = e20_4h > e50_4h && last4h.close > e20_4h;
  return {
    aligned: rising1h && rising4h,
    rising1h,
    rising4h,
    strength: e20_1h > 0 ? (e9_1h - e20_1h) / e20_1h : 0,
  };
}

function breakoutRetest(candles) {
  const start = Math.max(25, candles.length - 16);
  for (let i = start; i < candles.length - 1; i++) {
    const prior = candles.slice(i - 20, i);
    const resistance = Math.max(...prior.map(x => x.high));
    const volumeMedian = median(prior.map(x => x.quoteVolume));
    const b = candles[i];
    if (b.close <= resistance * 1.001 || b.quoteVolume < volumeMedian * 1.20) continue;
    for (let j = i + 1; j < candles.length; j++) {
      const r = candles[j];
      if (r.close < resistance * 0.99) break;
      if (r.low <= resistance * 1.006 && r.close >= resistance * 0.998) {
        const latest = candles.at(-1);
        if (latest.close >= resistance && latest.close <= resistance * 1.08) {
          return { type: "BREAKOUT_RETEST", resistance, retest: r, time: r.openTime };
        }
      }
    }
  }
  return null;
}

function pullbackBounce(candles) {
  const closes = candles.map(x => x.close);
  const e20 = emaSeries(closes, 20);
  const last = candles.at(-1), prev = candles.at(-2);
  const e = e20.at(-2);
  const touched = prev.low <= e * 1.006 && prev.close >= e * 0.995;
  const confirmed = last.close > prev.high && last.close > last.open;
  const volumeMedian = median(candles.slice(-21, -1).map(x => x.quoteVolume));
  const volumeOK = last.quoteVolume >= volumeMedian * 1.10;
  if (touched && confirmed && volumeOK) {
    return { type: "TREND_PULLBACK", trigger: last, retest: prev, time: last.openTime };
  }
  return null;
}

// TST requires its exact agreed sequence: a completed 15m close above 0.01855,
// followed by a later completed candle that tests and holds 0.01848-0.01855.
function tstBreakoutRetest(candles, currentAsk) {
  const breakoutLevel = 0.01855;
  const retestLow = 0.01848;
  const recent = candles.slice(-24);
  for (let i = 0; i < recent.length - 1; i++) {
    const b = recent[i];
    if (b.close <= breakoutLevel) continue;
    for (let j = i + 1; j < recent.length; j++) {
      const r = recent[j];
      const testedZone = r.low <= breakoutLevel && r.high >= retestLow;
      const heldZone = r.close >= retestLow;
      const entryInRange = currentAsk >= 0.01856 && currentAsk <= 0.01865;
      if (testedZone && heldZone && entryInRange) {
        return { type: "TST_EXACT_BREAKOUT_RETEST", resistance: breakoutLevel,
          retest: r, time: r.openTime };
      }
      if (r.close < retestLow) break;
    }
  }
  return null;
}

async function getPublicChannelMessages(channel) {
  const response = await fetch(`https://t.me/s/${channel}`, {
    headers: { Accept: "text/html", "User-Agent": "Mozilla/5.0 SignalResearchBot/1.0" },
    cf: { cacheTtl: 60, cacheEverything: true },
  });
  if (!response.ok) throw new Error(`${channel} fetch failed: ${response.status}`);
  const messages = [];
  const handler = {
    current: "",
    element(element) {
      this.current = "";
      element.onEndTag(() => {
        const value = this.current.replace(/\s+/g, " ").trim();
        if (value) messages.push(value);
        this.current = "";
      });
    },
    text(chunk) { this.current += `${chunk.text} `; },
  };
  const transformed = new HTMLRewriter()
    .on(".tgme_widget_message_text", handler)
    .transform(response);
  await transformed.text();
  return messages.filter(Boolean).slice(-CFG.telegramLookbackMessages).reverse();
}

async function getRiskSources() {
  const entries = await Promise.all(Object.entries(CFG.sourceChannels).map(async ([name, channel]) => {
    try { return [name, await getPublicChannelMessages(channel)]; }
    catch { return [name, []]; }
  }));
  return Object.fromEntries(entries);
}

function assessSourceRisk(sources, baseAsset, symbol) {
  const base = baseAsset.toUpperCase();
  const aliases = [base, symbol.toUpperCase(), `${base}/USDT`, `${base}USDT`];
  if (base === "BTC") aliases.push("BITCOIN");
  if (base === "ETH") aliases.push("ETHEREUM");
  if (base === "SOL") aliases.push("SOLANA");
  const matched = list => (list || []).find(raw => aliases.some(alias => containsToken(raw.toUpperCase(), alias)));

  const binanceMessage = matched(sources.binance);
  const binanceBlocked = Boolean(binanceMessage &&
    /DELIST|REMOVE.*TRADING|SUSPEND.*TRADING|CEASE.*TRADING|WILL NOT SUPPORT/i.test(binanceMessage));

  const cryptoMessage = (sources.cryptoQuant || []).find(raw =>
    /(BTC|BITCOIN).*(EXCHANGE INFLOW|INFLOW.*EXCHANGE|SELLING PRESSURE|SELL PRESSURE)/i.test(raw) &&
    /(SURGE|SPIKE|INCREAS|HIGH|BEARISH|RISK)/i.test(raw));

  const whaleMessage = matched(sources.whaleAlert);
  const whaleBlocked = Boolean(whaleMessage &&
    /(TO BINANCE|TRANSFERRED TO BINANCE|DEPOSITED.*BINANCE)/i.test(whaleMessage));

  return {
    binance: { blocked: binanceBlocked, matchedMessage: binanceMessage?.slice(0, 300) || null },
    cryptoQuant: { blocked: Boolean(cryptoMessage), matchedMessage: cryptoMessage?.slice(0, 300) || null },
    whaleAlert: { blocked: whaleBlocked, matchedMessage: whaleMessage?.slice(0, 300) || null },
  };
}

function containsToken(text, token) {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^A-Z0-9])${escaped}([^A-Z0-9]|$)`, "i").test(text);
}

function orderBookStats(depth, mid) {
  const bids = (depth.bids || [])
    .filter(([p]) => Number(p) >= mid * 0.99)
    .map(([p, q]) => Number(p) * Number(q));
  const asks = (depth.asks || [])
    .filter(([p]) => Number(p) <= mid * 1.01)
    .map(([p, q]) => Number(p) * Number(q));
  const bid = bids.reduce((sum, value) => sum + value, 0);
  const ask = asks.reduce((sum, value) => sum + value, 0);
  const largestAsk = asks.length ? Math.max(...asks) : ask;
  return {
    bid,
    ask,
    bidAskRatio: ask > 0 ? bid / ask : 0,
    largestAskShare: ask > 0 ? largestAsk / ask : 1,
  };
}

async function getBinanceProductMetadata() {
  const response = await fetch(PRODUCT_METADATA_URL, {
    headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-product-filter/1.0" },
    cf: { cacheTtl: 300, cacheEverything: true },
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`Binance product metadata failed: ${response.status}`);
  const payload = await response.json();
  const rows = Array.isArray(payload?.data) ? payload.data : [];
  return new Map(rows.map(row => [String(row.s || ""), {
    name: String(row.an || row.adn || ""),
    tags: Array.isArray(row.tags) ? row.tags : [],
    etf: Boolean(row.etf),
  }]));
}

async function binance(path) {
  let last = "unknown";
  try {
    const relay = `${CFG.defaultExecutorUrl}/market-data?path=${encodeURIComponent(path)}`;
    const r = await fetch(relay, {
      headers: { Accept: "application/json", "User-Agent": "tst-cloudflare-market-relay/1.0" },
      signal: AbortSignal.timeout(20_000),
    });
    if (r.ok) return await r.json();
    last = `Vercel market relay: ${r.status}`;
  } catch (e) {
    last = `Vercel market relay: ${e.message}`;
  }
  for (const base of API_BASES) {
    try {
      const r = await fetch(base + path, { headers: { "User-Agent": "Mozilla/5.0 spot-market-scanner" } });
      if (!r.ok) { last = `${base}: ${r.status}`; continue; }
      return await r.json();
    } catch (e) { last = `${base}: ${e.message}`; }
  }
  throw new Error(`Binance API failed (${last})`);
}

function candle(k) {
  return { openTime: +k[0], open: +k[1], high: +k[2], low: +k[3], close: +k[4],
    volume: +k[5], closeTime: +k[6], quoteVolume: +k[7],
    takerBuyBase: +k[9], takerBuyQuote: +k[10] };
}
function closed(x) { return x.filter(c => c.closeTime < Date.now()); }
function ema(values, period) {
  const k = 2 / (period + 1); let v = values[0];
  for (let i = 1; i < values.length; i++) v = values[i] * k + v * (1 - k);
  return v;
}
function emaSeries(values, period) {
  const k = 2 / (period + 1), out = [values[0]];
  for (let i = 1; i < values.length; i++) out.push(values[i] * k + out[i - 1] * (1 - k));
  return out;
}
function atr14(c) {
  const s = c.slice(-15), tr = [];
  for (let i = 1; i < s.length; i++) tr.push(Math.max(s[i].high - s[i].low,
    Math.abs(s[i].high - s[i - 1].close), Math.abs(s[i].low - s[i - 1].close)));
  return tr.reduce((a, b) => a + b, 0) / Math.max(tr.length, 1);
}
function rsi14(values) {
  if (values.length < 15) return 50;
  const recent = values.slice(-15);
  let gains = 0, losses = 0;
  for (let i = 1; i < recent.length; i++) {
    const change = recent[i] - recent[i - 1];
    if (change > 0) gains += change;
    else losses -= change;
  }
  if (losses === 0) return gains > 0 ? 100 : 50;
  const rs = gains / losses;
  return 100 - (100 / (1 + rs));
}
function median(v) {
  const s = [...v].sort((a, b) => a - b), m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
function floorStep(value, step) {
  const p = Math.max(0, Math.ceil(-Math.log10(step)));
  return Number((Math.floor(value / step) * step).toFixed(p));
}
function roundPrice(v, ref) {
  const d = ref < 0.01 ? 8 : ref < 0.1 ? 6 : ref < 1 ? 5 : ref < 10 ? 4 : 3;
  return Number(v.toFixed(d));
}
function round(v, d) { const p = 10 ** d; return Math.round(v * p) / p; }
function fmt(v) { return Number(v).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 }); }
function trim(v) { return Number(v.toFixed(8)).toString(); }

function alertText(x, regime) {
  const p = x.plan;
  return [
    `🚨 إشارة Paper مؤهلة: ${x.symbol.replace("USDT", "/USDT")}`,
    `درجة التأكيد: ${x.score}/100 — الحد الأدنى ${CFG.minScore}`,
    `نوع الفرصة: ${x.setup}`,
    `الدخول المشروط الآن: ${p.entry}`,
    `الكمية: ${p.quantity} — قيمة الصفقة: ${p.positionUSDT} USDT`,
    `وقف الخسارة: ${p.stop}`,
    `الهدف الأول: ${p.target1}`,
    `الهدف الثاني: ${p.target2}`,
    `المخاطرة شاملة الرسوم: ${p.riskIncludingFeesUSDT} USDT`,
    `الرسوم المتوقعة دخول + هدف أول: ${p.feesEntryAndTarget1USDT} USDT`,
    `R:R الصافي: ${p.netRewardRisk}`,
    `تغير 24س: ${x.market.change24hPct}% | السبريد: ${x.market.spreadPct}%`,
    `تأكيد الحجم: ${x.market.relativeVolume}x | ضغط شراء Taker: ${(Number(x.market.takerBuyRatio) * 100).toFixed(1)}%`,
    `دعم دفتر الأوامر Bid/Ask: ${x.market.bidAskDepthRatio}x | RSI: ${x.market.rsi15m}`,
    `العمر المرصود: ${x.market.ageDaysObserved}+ يوم | OCO قابل للتنفيذ: ${x.checks.protectedLegsMeetNotional ? "نعم" : "لا"}`,
    `حالة BTC: ${regime.state}`,
    `المصادر الرسمية: Binance آمن=${!x.officialSources.binance.blocked} | CryptoQuant آمن=${!x.officialSources.cryptoQuant.blocked} | Whale Alert آمن=${!x.officialSources.whaleAlert.blocked}`,
    `الإلغاء: ${p.invalidation}`,
    "⚠️ اختبار ورقي فقط، ولا توجد استراتيجية تضمن المكسب. لا يتم إرسال أي أمر إلى Binance.",
  ].join("\n");
}

async function telegram(env, text, replyMarkup = undefined) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) throw new Error("Telegram secrets missing");
  return telegramApi(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });
}

async function telegramApi(env, method, body) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("Telegram bot token missing");
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) throw new Error(`Telegram ${method} error ${r.status}: ${data.description || "unknown"}`);
  return data.result;
}

async function setupTelegramWebhook(request, env) {
  if (request.method !== "POST") return output({ ok: false, error: "POST required" }, 405);
  const supplied = request.headers.get("Authorization") || "";
  if (!env.RUN_TOKEN || supplied !== `Bearer ${env.RUN_TOKEN}`) return output({ ok: false, error: "Unauthorized" }, 401);
  const origin = new URL(request.url).origin;
  const secret = await getTelegramWebhookSecret(env);
  const result = await telegramApi(env, "setWebhook", {
    url: `${origin}/telegram-webhook`,
    secret_token: secret,
    allowed_updates: ["callback_query"],
    drop_pending_updates: false,
  });
  return output({ ok: true, webhook: `${origin}/telegram-webhook`, result, paperOnly: true });
}

async function ensureTelegramWebhook(env) {
  const secret = await getTelegramWebhookSecret(env);
  return telegramApi(env, "setWebhook", {
    url: `${CFG.publicBaseUrl}/telegram-webhook`,
    secret_token: secret,
    allowed_updates: ["callback_query"],
    drop_pending_updates: false,
  });
}

async function paperStatus(env) {
  try {
    const info = await telegramApi(env, "getWebhookInfo", {});
    const active = info.url === `${CFG.publicBaseUrl}/telegram-webhook`;
    return output({
      ok: true,
      paperOnly: true,
      liveTrading: false,
      telegramWebhookActive: active,
      pendingUpdateCount: Number(info.pending_update_count || 0),
      lastErrorDate: info.last_error_date || null,
      lastErrorMessage: info.last_error_message || null,
    }, active ? 200 : 503);
  } catch (error) {
    return output({ ok: false, paperOnly: true, liveTrading: false, error: String(error?.message || error) }, 500);
  }
}

async function getTelegramWebhookSecret(env) {
  if (env.TELEGRAM_WEBHOOK_SECRET) return env.TELEGRAM_WEBHOOK_SECRET;
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("Telegram bot token missing");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`paper-webhook:${env.TELEGRAM_BOT_TOKEN}`));
  return [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, "0")).join("");
}

async function telegramWebhook(request, env) {
  try {
    if (request.method !== "POST") return output({ ok: false, error: "POST required" }, 405);
    const expectedSecret = await getTelegramWebhookSecret(env);
    if (!expectedSecret || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== expectedSecret) {
      return output({ ok: false, error: "Unauthorized" }, 401);
    }

    const update = await request.json();
    const query = update.callback_query;
    if (!query) return output({ ok: true, ignored: true });

    const chatId = String(query.message?.chat?.id || "");
    if (chatId !== String(env.TELEGRAM_CHAT_ID)) {
      await telegramApi(env, "answerCallbackQuery", { callback_query_id: query.id, text: "غير مسموح", show_alert: true });
      return output({ ok: false, error: "Wrong chat" }, 403);
    }

    const data = String(query.data || "");
    const liveMatch = data.match(/^live_(buy|no):([A-Z0-9]{1,20}USDT):([a-f0-9]{8})$/);
    if (liveMatch) {
      const [, action, symbol, signalHash] = liveMatch;
      const cfg = liveConfig(env);
      const messageAgeMs = Math.max(0, Date.now() - Number(query.message?.date || 0) * 1000);

      if (messageAgeMs > cfg.approvalSeconds * 1000) {
        await telegramApi(env, "answerCallbackQuery", { callback_query_id: query.id, text: "الإشارة انتهت صلاحيتها", show_alert: true });
        await finalizeTelegramButton(env, query, "⌛ الإشارة انتهت صلاحيتها. استني إشارة جديدة.");
        return output({ ok: true, status: "LIVE_SIGNAL_EXPIRED", symbol });
      }

      if (action === "no") {
        await telegramApi(env, "answerCallbackQuery", { callback_query_id: query.id, text: "تم التجاهل" });
        await finalizeTelegramButton(env, query, `❌ تم تجاهل فرصة ${symbol.replace("USDT", "/USDT")}`);
        return output({ ok: true, status: "LIVE_REJECTED", symbol });
      }

      // Remove the button immediately to avoid accidental double taps.
      if (query.message?.message_id) {
        await telegramApi(env, "editMessageReplyMarkup", {
          chat_id: env.TELEGRAM_CHAT_ID,
          message_id: query.message.message_id,
          reply_markup: { inline_keyboard: [] },
        }).catch(() => null);
      }
      await telegramApi(env, "answerCallbackQuery", { callback_query_id: query.id, text: "جاري فحص السعر والحساب ثم تنفيذ Spot…" });

      const execution = await executeLiveSpotBuy(env, symbol, signalHash);
      if (!execution.ok) {
        await finalizeTelegramButton(env, query, [
          `⚠️ لم يتم الشراء — ${symbol.replace("USDT", "/USDT")}`,
          execution.reason || execution.error || "تعذر التنفيذ",
          "لم يتم إرسال صفقة جديدة إذا ظهر هذا التنبيه.",
        ].join("\n"));
        return output({ ...execution, symbol });
      }

      const lines = [
        `✅ تم شراء ${symbol.replace("USDT", "/USDT")} — SPOT`,
        `💵 المستخدم: ${execution.quoteSpentUSDT} USDT`,
        `📦 الكمية: ${execution.quantity}`,
        `💲 متوسط التنفيذ: ${execution.avgFillPrice}`,
        `🛑 Stop: ${execution.stop}`,
        `🎯 Take Profit: ${execution.takeProfit}`,
        `📂 الصفقات المفتوحة الآن: ${execution.openPositionsAfter}/${execution.maxOpenPositions}`,
        execution.protection === "OCO"
          ? "🛡️ الحماية مفعلة على Binance: Take Profit + Stop Loss (OCO)."
          : execution.protection === "STOP_ONLY"
            ? "🛡️ تم تفعيل Stop Loss فقط لأن OCO لم يُقبل."
            : "🛡️ تم إغلاق الصفقة فورًا كإجراء أمان لأن الحماية لم تُقبل.",
      ];
      await finalizeTelegramButton(env, query, lines.join("\n"));
      return output(execution);
    }

    // Keep the previous paper buttons working for the legacy paper scanner.
    await telegramApi(env, "answerCallbackQuery", { callback_query_id: query.id, text: "جاري إعادة فحص السوق…" });
    const match = data.match(/^paper_(yes|no):([a-f0-9]{20})$/);
    if (!match) return output({ ok: true, ignored: true });
    const [, action, approvalId] = match;
    const pending = await getState(env, `pending:${approvalId}`);
    if (!pending || pending.used || Date.now() > Number(pending.expiresAt)) {
      await finalizeTelegramButton(env, query, "⌛ انتهت صلاحية الإشارة الورقية أو تم استخدامها.");
      return output({ ok: true, status: "EXPIRED_OR_USED", paperOnly: true });
    }
    pending.used = true;
    await putState(env, `pending:${approvalId}`, pending, 180);
    if (action === "no") {
      await finalizeTelegramButton(env, query, `❌ تم رفض الصفقة الورقية: ${pending.symbol}`);
      return output({ ok: true, status: "REJECTED", symbol: pending.symbol, paperOnly: true });
    }
    const daily = await getDailyPaper(env);
    if (daily.realizedPnlUSDT <= -CFG.dailyLossCap) {
      await finalizeTelegramButton(env, query, `🛑 مرفوض: تم بلوغ حد الخسارة الورقي اليومي ${CFG.dailyLossCap} USDT.`);
      return output({ ok: true, status: "DAILY_LOSS_CAP", paperOnly: true });
    }
    const activeBeforeOpen = (await getState(env, "paper:active") || []).filter(Boolean);
    if (activeBeforeOpen.length >= CFG.maxOpenPaperPositions) {
      await finalizeTelegramButton(env, query, "🛑 لم تُفتح الصفقة الورقية: يوجد بالفعل مركز مفتوح. ننتظر إغلاقه أولًا.");
      return output({ ok: true, status: "MAX_OPEN_PAPER_POSITIONS", paperOnly: true });
    }
    const fresh = await revalidateCandidate(pending.symbol);
    if (!fresh.valid) {
      await finalizeTelegramButton(env, query, `⚠️ لم تُنفذ ورقيًا: شروط ${pending.symbol} لم تعد صالحة بعد إعادة الفحص.\nالسبب: ${fresh.reason}`);
      return output({ ok: true, status: "REVALIDATION_FAILED", symbol: pending.symbol, reason: fresh.reason, paperOnly: true });
    }
    const position = {
      id: approvalId,
      symbol: fresh.candidate.symbol,
      openedAt: Date.now(),
      entry: Number(fresh.candidate.plan.entry),
      stop: Number(fresh.candidate.plan.stop),
      target1: Number(fresh.candidate.plan.target1),
      target2: Number(fresh.candidate.plan.target2),
      quantity: Number(fresh.candidate.plan.quantity),
      positionUSDT: Number(fresh.candidate.plan.positionUSDT),
      entryFeeUSDT: Number(fresh.candidate.plan.positionUSDT) * CFG.fee,
      status: "OPEN",
    };
    await putState(env, `position:${position.id}`, position, CFG.paperPositionHours * 3600);
    const active = await getState(env, "paper:active") || [];
    if (!active.includes(position.id)) active.push(position.id);
    await putState(env, "paper:active", active, CFG.paperPositionHours * 3600);
    await finalizeTelegramButton(env, query, [
      `✅ تم فتح صفقة ورقية: ${position.symbol}`,
      `الدخول الافتراضي: ${fmt(position.entry)}`,
      `الكمية: ${trim(position.quantity)} — القيمة: ${position.positionUSDT} USDT`,
      `الوقف: ${fmt(position.stop)}`,
      `الهدف 1: ${fmt(position.target1)} | الهدف 2: ${fmt(position.target2)}`,
      "لا يوجد أمر حقيقي على Binance.",
    ].join("\n"));
    return output({ ok: true, status: "PAPER_POSITION_OPENED", position, paperOnly: true, liveTrading: false });
  } catch (error) {
    return output({ ok: false, error: String(error?.message || error) }, 500);
  }
}

async function finalizeTelegramButton(env, query, text) {
  if (query.message?.message_id) {
    return telegramApi(env, "editMessageText", {
      chat_id: env.TELEGRAM_CHAT_ID,
      message_id: query.message.message_id,
      text,
      reply_markup: { inline_keyboard: [] },
    });
  }
  return telegram(env, text);
}

async function revalidateCandidate(symbol) {
  try {
    const [tickers, books, exchangeInfo, products, btc1h, btc4h, riskSources] = await Promise.all([
      binance(`/api/v3/ticker/24hr?symbol=${symbol}`),
      binance(`/api/v3/ticker/bookTicker?symbol=${symbol}`),
      binance("/api/v3/exchangeInfo"),
      getBinanceProductMetadata().catch(() => new Map()),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=100"),
      getRiskSources().catch(() => ({ binance: [], cryptoQuant: [], whaleAlert: [] })),
    ]);
    const info = exchangeInfo.symbols.find(x => x.symbol === symbol && x.status === "TRADING" && x.isSpotTradingAllowed && x.ocoAllowed);
    if (!info) return { valid: false, reason: "الزوج غير متاح Spot حاليًا" };
    const regime = marketRegime(btc1h.map(candle), btc4h.map(candle));
    if (!regime.longAllowed) return { valid: false, reason: "اتجاه BTC أصبح غير مناسب" };
    const summary = { symbol, base: info.baseAsset, volume: Number(tickers.quoteVolume), change: Number(tickers.priceChangePercent) };
    if (!isAllowedProduct(summary, products.get(symbol))) {
      return { valid: false, reason: "الزوج ليس عملة Crypto Spot مؤهلة أو مصنف bStocks/Leveraged" };
    }
    const candidate = await analyzeSymbol(summary, info, books, regime, riskSources);
    if (!candidate.valid) {
      const failed = Object.entries(candidate.checks || {}).filter(([, value]) => !value).map(([key]) => key).join(", ");
      return { valid: false, reason: failed || candidate.status || "فشل التحقق", candidate };
    }
    return { valid: true, candidate, regime };
  } catch (error) {
    return { valid: false, reason: String(error?.message || error) };
  }
}

async function monitorPaperPositions(env) {
  try {
    const active = await getState(env, "paper:active") || [];
    if (!active.length) return { checked: 0, closed: 0 };
    let closedCount = 0;
    const remaining = [];
    for (const id of active) {
      const position = await getState(env, `position:${id}`);
      if (!position || position.status !== "OPEN") continue;
      const book = await binance(`/api/v3/ticker/bookTicker?symbol=${position.symbol}`);
      const exit = Number(book.bidPrice);
      let reason = null;
      if (exit <= position.stop) reason = "STOP";
      else if (exit >= position.target2) reason = "TARGET_2";
      else if (exit >= position.target1) reason = "TARGET_1";
      else if (Date.now() - Number(position.openedAt || 0) >= CFG.maxHoldHours * 3600 * 1000) reason = "TIME_EXIT";
      if (!reason) { remaining.push(id); continue; }
      const exitFee = position.quantity * exit * CFG.fee;
      const pnl = position.quantity * (exit - position.entry) - position.entryFeeUSDT - exitFee;
      position.status = "CLOSED";
      position.closedAt = Date.now();
      position.exit = exit;
      position.exitReason = reason;
      position.realizedPnlUSDT = round(pnl, 4);
      await putState(env, `position:${id}`, position, CFG.paperPositionHours * 3600);
      const daily = await getDailyPaper(env);
      daily.realizedPnlUSDT = round(daily.realizedPnlUSDT + pnl, 4);
      daily.closedTrades += 1;
      await putState(env, daily.key, daily, 3 * 24 * 3600);
      await telegram(env, `📒 إغلاق ورقي ${position.symbol}\nالسبب: ${reason}\nالخروج الافتراضي: ${fmt(exit)}\nصافي النتيجة بعد الرسوم: ${position.realizedPnlUSDT} USDT\nإجمالي اليوم: ${daily.realizedPnlUSDT} USDT`);
      closedCount++;
    }
    await putState(env, "paper:active", remaining, CFG.paperPositionHours * 3600);
    return { checked: active.length, closed: closedCount };
  } catch (error) {
    return { checked: 0, closed: 0, error: String(error?.message || error) };
  }
}

async function getDailyPaper(env) {
  const day = new Date().toISOString().slice(0, 10);
  const key = `paper:daily:${day}`;
  return await getState(env, key) || { key, day, realizedPnlUSDT: 0, closedTrades: 0 };
}

function liveConfig(env) {
  const num = (value, fallback, min, max) => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
  };
  return {
    enabled: !CFG.validationMode && !["1", "true", "yes", "on"].includes(String(env.LIVE_TRADING_KILL_SWITCH || "").toLowerCase()),
    // Human confirmation is mandatory: a valid setup sends a Telegram BUY button.
    autoExecute: false,
    compoundEnabled: ["1", "true", "yes", "on"].includes(String(env.COMPOUND_ENABLED || "false").toLowerCase()),
    // Environment settings may only reduce the agreed hard safety limits.
    tradeUSDT: num(env.TRADE_USDT, CFG.maxPosition, 1, CFG.maxPosition),
    tradeEquityPct: num(env.TRADE_EQUITY_PCT, 25, 5, 50),
    maxTradeUSDT: num(env.MAX_TRADE_USDT, CFG.maxPosition, 1, CFG.maxPosition),
    maxOpenPositions: Math.floor(num(env.MAX_OPEN_POSITIONS, 1, 1, 1)),
    reserveUSDT: num(env.RESERVE_USDT, 2, 0, 100000),
    maxRiskUSDT: num(env.MAX_RISK_USDT, CFG.maxRisk, 0.01, CFG.maxRisk),
    dailyLossCapUSDT: num(env.DAILY_LOSS_CAP_USDT, CFG.dailyLossCap, 0.1, CFG.dailyLossCap),
    approvalSeconds: Math.floor(num(env.APPROVAL_SECONDS, 90, 30, 300)),
    earlyWarningMinutes: Math.floor(num(env.EARLY_WARNING_MINUTES, 15, 5, 60)),
    capitalCapEGP: num(env.CAPITAL_CAP_EGP, 3000, 100, 1000000),
    egpPerUSDT: num(env.EGP_PER_USDT, 51, 1, 1000),
  };
}

function hasBinanceKeys(_env) {
  return false;
}

async function portfolioStatus(env) {
  const cfg = liveConfig(env);
  try {
    const executorBase = String(env.EXECUTOR_URL || CFG.defaultExecutorUrl).replace(/\/$/, "");
    const r = await fetch(`${executorBase}/executor/status`, {
      headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-cloudflare-status/1.0" },
      signal: AbortSignal.timeout(10_000),
    });
    const status = await r.json();
    return output({
      ok: r.ok,
      mode: "PORTFOLIO_MANAGER",
      executor: status,
      config: {
        tradeUSDT: cfg.tradeUSDT,
        compoundEnabled: cfg.compoundEnabled,
        tradeEquityPct: cfg.tradeEquityPct,
        maxTradeUSDT: cfg.maxTradeUSDT,
        autoExecute: cfg.autoExecute,
        maxOpenPositions: cfg.maxOpenPositions,
        reserveUSDT: cfg.reserveUSDT,
        maxRiskUSDT: cfg.maxRiskUSDT,
        dailyLossCapUSDT: cfg.dailyLossCapUSDT,
        earlyWarningMinutes: cfg.earlyWarningMinutes,
        capitalCapEGP: cfg.capitalCapEGP,
        egpPerUSDT: cfg.egpPerUSDT,
      },
    }, r.ok ? 200 : 503);
  } catch (error) {
    return output({ ok: false, mode: "PORTFOLIO_MANAGER", error: String(error?.message || error) }, 503);
  }
}



async function livePreflight(env) {
  const cacheKey = new Request("https://preflight-cache.local/binance-spot");
  if (await caches.default.match(cacheKey)) {
    return output({ ok: false, status: "PREFLIGHT_RATE_LIMITED", orderPlaced: false, fundsUsed: false }, 429);
  }
  await caches.default.put(cacheKey, new Response("running", {
    headers: { "Cache-Control": "max-age=60" },
  }));

  if (!env.TELEGRAM_BOT_TOKEN) {
    return output({ ok: false, status: "RELAY_SECRET_MISSING", orderPlaced: false, fundsUsed: false }, 503);
  }

  const executorBase = String(env.EXECUTOR_URL || CFG.defaultExecutorUrl).replace(/\/$/, "");
  const body = JSON.stringify({ symbol: "BTCUSDT", quoteOrderQty: 5, timestamp: Date.now() });
  const ts = String(Date.now());
  const signature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);

  try {
    const response = await fetch(`${executorBase}/executor/preflight`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Executor-Timestamp": ts,
        "X-Executor-Signature": signature,
        "User-Agent": "tst-spot-signal-cloudflare-preflight/1.0",
      },
      body,
      signal: AbortSignal.timeout(20_000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      const reason = data.reason || data.error || `Executor returned ${response.status}`;
      await telegram(env, [
        "❌ اختبار Binance Spot فشل",
        reason,
        "",
        "لم يتم إنشاء أي أمر ولم تُخصم أي أموال."
      ].join("\n"));
      return output({ ok: false, status: data.status || "PREFLIGHT_FAILED", reason, orderPlaced: false, fundsUsed: false }, 409);
    }

    await telegram(env, [
      "✅ اختبار Binance Spot نجح",
      `تم قبول اختبار أمر بقيمة ${data.testedQuoteUSDT} USDT على ${data.symbol}.`,
      `الرصيد الحر الذي قرأه Binance: ${data.freeUSDT} USDT`,
      "",
      "ده order/test فقط: لم يتم إنشاء صفقة ولم تُخصم أي أموال.",
      "النظام جاهز لأول إشارة BUY حقيقية."
    ].join("\n"));
    return output(data);
  } catch (error) {
    const reason = String(error?.message || error);
    await telegram(env, `❌ اختبار Binance Spot تعذر: ${reason}\nلم يتم إنشاء أي أمر ولم تُخصم أي أموال.`).catch(() => null);
    return output({ ok: false, status: "PREFLIGHT_UNREACHABLE", reason, orderPlaced: false, fundsUsed: false }, 503);
  }
}


async function executeLiveSpotBuy(env, symbol, signalHash) {
  const cfg = liveConfig(env);
  const daily = await getLiveDailyRisk(env);
  const pnl = await getLiveDailyPnl(env);
  if (daily.trades >= CFG.maxLiveTradesPerDay) {
    return {
      ok: false,
      status: "DAILY_TRADE_LIMIT",
      reason: `وصلنا للحد اليومي ${CFG.maxLiveTradesPerDay} صفقات.`,
    };
  }
  if (pnl.netPnlUSDT <= -cfg.dailyLossCapUSDT) {
    return {
      ok: false,
      status: "DAILY_LOSS_CAP",
      reason: `وصلنا لحد الخسارة اليومي ${cfg.dailyLossCapUSDT} USDT.`,
    };
  }
  if (!env.TELEGRAM_BOT_TOKEN) {
    return { ok: false, status: "RELAY_SECRET_MISSING", reason: "Telegram relay secret is missing." };
  }

  const executorBase = String(env.EXECUTOR_URL || CFG.defaultExecutorUrl).replace(/\/$/, "");
  const endpoint = `${executorBase}/execute/spot-buy`;
  const body = JSON.stringify({
    action: "BUY",
    symbol,
    signalHash,
    timestamp: Date.now(),
    sizing: {
      compoundEnabled: cfg.compoundEnabled,
      baseTradeUSDT: cfg.tradeUSDT,
      equityPct: cfg.tradeEquityPct,
      maxTradeUSDT: cfg.maxTradeUSDT,
      reserveUSDT: cfg.reserveUSDT,
      maxRiskUSDT: cfg.maxRiskUSDT,
      maxOpenPositions: cfg.maxOpenPositions
    }
  });
  const ts = String(Date.now());
  const signature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);

  let response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Executor-Timestamp": ts,
        "X-Executor-Signature": signature,
        "User-Agent": "tst-spot-signal-cloudflare-relay/1.0",
      },
      body,
      signal: AbortSignal.timeout(30_000),
    });
  } catch (error) {
    return { ok: false, status: "EXECUTOR_UNREACHABLE", reason: String(error?.message || error) };
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) {
    return {
      ok: false,
      status: data.status || "EXECUTOR_REJECTED",
      reason: data.reason || data.error || `Vercel executor returned ${response.status}`,
    };
  }

  if (Number(data.riskUSDT) > 0) await addLiveDailyRisk(env, Number(data.riskUSDT));
  if (data.status === "LIVE_SPOT_OPENED" && Number(data.orderId) > 0) {
    await trackLivePosition(env, data);
  }
  return data;
}

async function trackLivePosition(env, execution) {
  const id = `${execution.symbol}:${execution.orderId}`;
  const active = await getState(env, "live:positions:active") || [];
  if (!active.includes(id)) active.push(id);
  await putState(env, `live:position:${id}`, {
    id,
    symbol: execution.symbol,
    entryOrderId: Number(execution.orderId),
    entryPrice: execution.avgFillPrice,
    quantity: execution.quantity,
    quoteSpentUSDT: execution.quoteSpentUSDT,
    openedAt: Date.now(),
    status: "OPEN",
  }, 30 * 24 * 3600);
  await putState(env, "live:positions:active", active, 30 * 24 * 3600);
}

async function discoverLivePositions(env) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  try {
    const body = JSON.stringify({ timestamp: Date.now() });
    const ts = String(Date.now());
    const signature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
    const executorBase = String(env.EXECUTOR_URL || CFG.defaultExecutorUrl).replace(/\/$/, "");
    const response = await fetch(`${executorBase}/executor/discover-open-positions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Executor-Timestamp": ts,
        "X-Executor-Signature": signature,
      },
      body,
      signal: AbortSignal.timeout(20_000),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) return;
    for (const position of data.positions || []) {
      await trackLivePosition(env, {
        symbol: position.symbol,
        orderId: position.entryOrderId,
        avgFillPrice: position.entryPrice,
        quantity: position.quantity,
        quoteSpentUSDT: position.quoteSpentUSDT,
      });
    }
  } catch {
    // Discovery is retried by the next scheduled run.
  }
}

async function monitorLivePositions(env) {
  try {
    await discoverLivePositions(env);
    const active = await getState(env, "live:positions:active") || [];
    if (!active.length || !env.TELEGRAM_BOT_TOKEN) return { checked: 0, closed: 0 };
    const remaining = [];
    let closed = 0;

    for (const id of active) {
      const position = await getState(env, `live:position:${id}`);
      if (!position || position.status !== "OPEN") continue;
      const body = JSON.stringify({
        symbol: position.symbol,
        entryOrderId: position.entryOrderId,
        timestamp: Date.now(),
      });
      const ts = String(Date.now());
      const signature = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
      const executorBase = String(env.EXECUTOR_URL || CFG.defaultExecutorUrl).replace(/\/$/, "");
      let data;
      try {
        const response = await fetch(`${executorBase}/executor/position-status`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Executor-Timestamp": ts,
            "X-Executor-Signature": signature,
          },
          body,
          signal: AbortSignal.timeout(20_000),
        });
        data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.reason || `HTTP ${response.status}`);
      } catch {
        remaining.push(id);
        continue;
      }

      if (!data.closed) {
        remaining.push(id);
        continue;
      }

      const closedKey = `live:closed-notified:${data.symbol}:${data.exitOrderId}`;
      if (!(await getState(env, closedKey))) {
        const day = new Date(Number(data.closedAt || Date.now())).toISOString().slice(0, 10);
        const pnlKey = `live:pnl:${day}`;
        const daily = await getState(env, pnlKey) || { day, netPnlUSDT: 0, wins: 0, losses: 0, trades: 0 };
        const pnl = Number(data.netPnlUSDT || 0);
        daily.netPnlUSDT = Math.round((Number(daily.netPnlUSDT || 0) + pnl) * 100000) / 100000;
        daily.trades += 1;
        if (pnl > 0) daily.wins += 1;
        else if (pnl < 0) daily.losses += 1;
        await putState(env, pnlKey, daily, 7 * 24 * 3600);
        await putState(env, closedKey, { notifiedAt: Date.now() }, 30 * 24 * 3600);

        const won = pnl >= 0;
        await telegram(env, [
          won ? `✅ الصفقة كسبت — ${data.symbol.replace("USDT", "/USDT")}` : `🔴 الصفقة خسرت — ${data.symbol.replace("USDT", "/USDT")}`,
          `سبب الإغلاق: ${data.reason === "TAKE_PROFIT" ? "Take Profit" : data.reason === "STOP_LOSS" ? "Stop Loss" : "إغلاق حماية"}`,
          `سعر الدخول: ${data.entryPrice}`,
          `سعر الخروج: ${data.exitPrice}`,
          `الكمية: ${data.quantity}`,
          `رسوم Binance: ${data.feesUSDT} USDT`,
          `صافي النتيجة: ${pnl >= 0 ? "+" : ""}${pnl.toFixed(5)} USDT`,
          "",
          `إجمالي اليوم: ${daily.netPnlUSDT >= 0 ? "+" : ""}${daily.netPnlUSDT.toFixed(5)} USDT`,
          `صفقات اليوم: ${daily.trades} | كسب: ${daily.wins} | خسارة: ${daily.losses}`,
        ].join("\n"));
      }

      position.status = "CLOSED";
      position.closedAt = Number(data.closedAt || Date.now());
      position.netPnlUSDT = Number(data.netPnlUSDT || 0);
      position.exitReason = data.reason;
      await putState(env, `live:position:${id}`, position, 30 * 24 * 3600);
      closed += 1;
    }

    await putState(env, "live:positions:active", remaining, 30 * 24 * 3600);
    return { checked: active.length, closed };
  } catch (error) {
    return { checked: 0, closed: 0, error: String(error?.message || error) };
  }
}

async function getAutoStats(env) {
  return await getState(env, "live:auto:stats") || {
    startedAt: Date.now(),
    entries: 0,
    lastSymbol: null,
    lastExecutionAt: null,
    lastKnownEquityUSDT: null,
    baselineEquityUSDT: null,
    lastKnownPnlUSDT: null
  };
}

async function recordAutoExecution(env, execution) {
  const stats = await getAutoStats(env);
  stats.entries = Number(stats.entries || 0) + 1;
  stats.lastSymbol = execution.symbol || stats.lastSymbol;
  stats.lastExecutionAt = Date.now();

  const equity = Number(execution.equityUSDT);
  if (Number.isFinite(equity) && equity > 0) {
    if (!(Number(stats.baselineEquityUSDT) > 0)) stats.baselineEquityUSDT = equity;
    stats.lastKnownEquityUSDT = equity;
    stats.lastKnownPnlUSDT = round2(equity - Number(stats.baselineEquityUSDT));
  }
  await putState(env, "live:auto:stats", stats, 365 * 24 * 3600);
  return stats;
}

async function sendCapitalConstraintAlert(env, symbol, execution) {
  const key = `capital-alert:${symbol}:${new Date().toISOString().slice(0, 13)}`;
  if (await getState(env, key)) return;
  await putState(env, key, { sentAt: Date.now() }, 3600);
  await telegram(env, [
    `💡 فيه فرصة مؤهلة إضافية: ${symbol.replace("USDT", "/USDT")}`,
    "لكن الرصيد/عدد الصفقات الحالي مش سامح بدخولها تحت حدود الأمان.",
    execution?.freeUSDT != null ? `الرصيد الحر: ${execution.freeUSDT} USDT` : null,
    execution?.requiredUSDT != null ? `المطلوب تقريبًا حسب الإعدادات: ${execution.requiredUSDT} USDT` : null,
    "",
    "مش مطلوب تزودي فلوس؛ لو حابة تزودي رأس المال، قوليلي ونحسب الحجم الجديد قبل أي إيداع."
  ].filter(Boolean).join("\n"));
}

async function emergencyClose(env, symbol, quantity, token) {
  if (!(Number(quantity) > 0)) return null;
  return signedBinance(env, "POST", "/api/v3/order", {
    symbol,
    side: "SELL",
    type: "MARKET",
    quantity,
    newClientOrderId: `TSTX${token}`,
    newOrderRespType: "FULL",
  });
}

async function getLiveDailyRisk(env) {
  const day = new Date().toISOString().slice(0, 10);
  const key = `live:risk:${day}`;
  return await getState(env, key) || { day, riskUsedUSDT: 0, trades: 0 };
}

async function getLiveDailyPnl(env) {
  const day = new Date().toISOString().slice(0, 10);
  return await getState(env, `live:pnl:${day}`) || { day, netPnlUSDT: 0, wins: 0, losses: 0, trades: 0 };
}

async function addLiveDailyRisk(env, riskUSDT) {
  const day = new Date().toISOString().slice(0, 10);
  const key = `live:risk:${day}`;
  const state = await getLiveDailyRisk(env);
  state.riskUsedUSDT = round2(Number(state.riskUsedUSDT || 0) + Number(riskUSDT || 0));
  state.trades = Number(state.trades || 0) + 1;
  await putState(env, key, state, 36 * 3600);
  return state;
}

async function signedBinance(env, method, path, params = {}) {
  if (!hasBinanceKeys(env)) throw new Error("Binance API keys are missing");
  const all = { ...params, recvWindow: 5000, timestamp: Date.now() };
  const query = Object.entries(all)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");
  const signature = await hmacHex(env.BINANCE_API_SECRET, query);
  const url = `https://api.binance.com${path}?${query}&signature=${signature}`;
  const r = await fetch(url, {
    method,
    headers: { "X-MBX-APIKEY": env.BINANCE_API_KEY, Accept: "application/json" },
    signal: AbortSignal.timeout(15_000),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${data.code || r.status}: ${data.msg || "Binance request failed"}`);
  return data;
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
  if (dot < 0) return 0;
  return s.length - dot - 1;
}

function floorToStep(value, step) {
  if (!(step > 0)) return String(value);
  const decimals = stepDecimals(step);
  const n = Math.floor((Number(value) + step * 1e-9) / step) * step;
  return n.toFixed(Math.min(decimals, 12)).replace(/\.?0+$/, "");
}

function ceilToStep(value, step) {
  if (!(step > 0)) return String(value);
  const decimals = stepDecimals(step);
  const n = Math.ceil((Number(value) - step * 1e-9) / step) * step;
  return n.toFixed(Math.min(decimals, 12)).replace(/\.?0+$/, "");
}

function round2(value) {
  return Math.round(Number(value) * 100) / 100;
}

async function getState(env, key) {
  if (env.SIGNAL_STATE) return env.SIGNAL_STATE.get(key, "json");
  const response = await caches.default.match(new Request(`https://paper-state.local/${encodeURIComponent(key)}`));
  return response ? response.json() : null;
}

async function putState(env, key, value, ttlSeconds) {
  if (env.SIGNAL_STATE) {
    await env.SIGNAL_STATE.put(key, JSON.stringify(value), { expirationTtl: Math.max(60, Math.floor(ttlSeconds)) });
    return;
  }
  await caches.default.put(
    new Request(`https://paper-state.local/${encodeURIComponent(key)}`),
    new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json", "Cache-Control": `max-age=${Math.max(60, Math.floor(ttlSeconds))}` } })
  );
}

async function relayTelegram(request, env) {
  try {
    if (request.method !== "POST") return output({ ok: false, error: "POST required" }, 405);
    if (!env.BINANCE_DEMO_SECRET_KEY) return output({ ok: false, error: "Relay secret missing" }, 503);
    const raw = await request.text();
    if (raw.length > 12000) return output({ ok: false, error: "Payload too large" }, 413);
    const supplied = request.headers.get("X-Relay-Signature") || "";
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.BINANCE_DEMO_SECRET_KEY), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    const bytes = supplied.match(/^[a-f0-9]{64}$/i) ? new Uint8Array(supplied.match(/../g).map(x => parseInt(x, 16))) : new Uint8Array();
    const valid = bytes.length === 32 && await crypto.subtle.verify("HMAC", key, bytes, new TextEncoder().encode(raw));
    if (!valid) return output({ ok: false, error: "Invalid signature" }, 401);
    const body = JSON.parse(raw);
    if (Math.abs(Date.now() - Number(body.timestamp)) > 5 * 60 * 1000) return output({ ok: false, error: "Expired request" }, 401);
    const message = String(body.text || "").trim();
    if (!message || message.length > 4000) return output({ ok: false, error: "Invalid message" }, 400);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(message));
    const id = [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, "0")).join("");
    const cacheKey = new Request(`https://relay-cache.local/${id}`);
    if (await caches.default.match(cacheKey)) return output({ ok: true, duplicate: true, sent: false });
    await telegram(env, message);
    await caches.default.put(cacheKey, new Response("sent", { headers: { "Cache-Control": `max-age=${CFG.duplicateHours * 3600}` } }));
    return output({ ok: true, sent: true });
  } catch (error) {
    return output({ ok: false, error: String(error?.message || error) }, 500);
  }
}

function resultBase(extra) {
  return { ok: true, checkedAt: new Date().toISOString(), mode: "SIGNAL_ONLY",
    liveTrading: false, orderPlaced: false, capitalUSDT: CFG.capital,
    maxPositionUSDT: CFG.maxPosition, maxRiskUSDT: CFG.maxRisk,
    dailyLossCapUSDT: CFG.dailyLossCap, ...extra };
}
function output(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
