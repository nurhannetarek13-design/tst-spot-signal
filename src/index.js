const API_BASES = [
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://data-api.binance.vision",
];

const CFG = {
  capital: 20.08,
  maxPosition: 5,
  maxRisk: 0.50,
  dailyLossCap: 2,
  fee: 0.001,
  minVolume24h: 2_000_000,
  minDepthEachSide: 10_000,
  maxSpreadPct: 0.50,
  maxRise24hPct: 25,
  maxStopPct: 8,
  minNetRR: 2,
  scanCount: 18,
  duplicateHours: 6,
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.searchParams.get("relay") === "telegram") {
      return relayTelegram(request, env);
    }
    if (url.searchParams.get("test") === "telegram") {
      await telegram(env, "✅ Binance Spot Market Scanner شغال — إشارات فقط، بدون تنفيذ صفقات.");
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
    ctx.waitUntil(scan(env));
  },
};

async function scan(env) {
  try {
    const dailyLoss = Number(env.DAILY_LOSS_USDT || 0);
    if (dailyLoss >= CFG.dailyLossCap) {
      return resultBase({ status: "DAILY_LOSS_CAP", dailyLossUSDT: dailyLoss });
    }

    const [tickers, books, exchangeInfo, btc1h, btc4h, riskSources] = await Promise.all([
      binance("/api/v3/ticker/24hr"),
      binance("/api/v3/ticker/bookTicker"),
      binance("/api/v3/exchangeInfo"),
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
        .filter(s => s.status === "TRADING" && s.quoteAsset === "USDT" && s.isSpotTradingAllowed)
        .map(s => [s.symbol, s])
    );
    const bookMap = new Map(books.map(b => [b.symbol, b]));
    const candidates = shortlist(tickers, tradable, bookMap);
    const analyses = [];

    for (let i = 0; i < candidates.length; i += 5) {
      const group = candidates.slice(i, i + 5);
      const groupResults = await Promise.all(
        group.map(x => analyzeSymbol(x, tradable.get(x.symbol), bookMap.get(x.symbol), regime, riskSources))
      );
      analyses.push(...groupResults);
    }

    const valid = analyses.filter(x => x.valid).sort((a, b) => b.score - a.score);
    const best = valid[0] || null;
    let alertSent = false;

    if (best) {
      const cache = caches.default;
      const cacheKey = new Request(`https://scanner-cache.local/${best.symbol}/${best.signalId}`);
      if (!(await cache.match(cacheKey))) {
        await telegram(env, alertText(best, regime));
        await cache.put(cacheKey, new Response("sent", {
          headers: { "Cache-Control": `max-age=${CFG.duplicateHours * 3600}` },
        }));
        alertSent = true;
      }
    }

    return resultBase({
      status: best ? "VALID_SETUP_FOUND" : "NO_VALID_SETUP",
      marketRegime: regime,
      candidatesScanned: candidates.map(x => x.symbol),
      alertSent,
      selected: best,
      results: analyses,
    });
  } catch (error) {
    return { ok: false, error: String(error?.message || error), liveTrading: false, orderPlaced: false };
  }
}

function shortlist(tickers, tradable, bookMap) {
  const eligible = tickers
    .filter(t => tradable.has(t.symbol) && bookMap.has(t.symbol))
    .map(t => ({
      symbol: t.symbol,
      base: tradable.get(t.symbol).baseAsset,
      volume: Number(t.quoteVolume),
      change: Number(t.priceChangePercent),
      trades: Number(t.count || 0),
    }))
    .filter(x => isAllowedBase(x.base))
    .filter(x => x.volume >= CFG.minVolume24h)
    .filter(x => x.change >= -8 && x.change <= CFG.maxRise24hPct);

  const liquid = [...eligible].sort((a, b) => b.volume - a.volume).slice(0, 10);
  const active = [...eligible]
    .filter(x => x.change > 0)
    .sort((a, b) => (b.change * Math.log10(b.volume)) - (a.change * Math.log10(a.volume)))
    .slice(0, 12);
  const priority = eligible.filter(x => x.symbol === "TSTUSDT" || x.symbol === "VIRTUALUSDT");
  const unique = new Map([...priority, ...liquid, ...active].map(x => [x.symbol, x]));
  return [...unique.values()].slice(0, CFG.scanCount);
}

function isAllowedBase(base) {
  if (EXCLUDED_BASES.has(base)) return false;
  if (/UP$|DOWN$|BULL$|BEAR$/.test(base)) return false;
  return true;
}

async function analyzeSymbol(summary, symbolInfo, book, regime, riskSources = {}) {
  try {
    const [raw15, raw1h, depth] = await Promise.all([
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=15m&limit=120`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1h&limit=100`),
      binance(`/api/v3/depth?symbol=${summary.symbol}&limit=100`),
    ]);
    const c15 = closed(raw15.map(candle));
    const c1h = closed(raw1h.map(candle));
    const bid = Number(book.bidPrice);
    const ask = Number(book.askPrice);
    const mid = (bid + ask) / 2;
    const spread = ((ask - bid) / mid) * 100;
    const depthSides = onePctDepth(depth, mid);
    const trend = symbolTrend(c1h);
    const breakout = summary.symbol === "TSTUSDT" ? tstBreakoutRetest(c15, ask) : breakoutRetest(c15);
    const pullback = summary.symbol !== "TSTUSDT" && trend.rising ? pullbackBounce(c15) : null;
    const setup = breakout || pullback;
    const atr = atr14(c15);
    const entry = ask;
    const swingLow = setup?.retest?.low || setup?.trigger?.low || c15.at(-2).low;
    const stopRaw = Math.min(swingLow * 0.998, entry - 1.15 * atr);
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
    const target1 = roundPrice(entry + 2.25 * riskUnit, entry);
    const target2 = roundPrice(entry + 3 * riskUnit, entry);
    const exitFee1 = quantity * target1 * CFG.fee;
    const netReward1 = quantity * (target1 - entry) - entryFee - exitFee1;
    const netRR = riskFees > 0 ? netReward1 / riskFees : 0;

    const checks = {
      btcMarketAllowsLongs: regime.longAllowed,
      volume24hAbove2M: summary.volume >= CFG.minVolume24h,
      notOverextended24h: summary.change <= CFG.maxRise24hPct,
      oneHourTrendRising: trend.rising,
      confirmedSetup: Boolean(setup),
      spreadBelow05Pct: spread < CFG.maxSpreadPct,
      bidDepthSufficient: depthSides.bid >= CFG.minDepthEachSide,
      askDepthSufficient: depthSides.ask >= CFG.minDepthEachSide,
      stopBelowEntry: stop > 0 && stop < entry,
      stopWithin8Pct: stopPct > 0 && stopPct <= CFG.maxStopPct,
      minimumOrderMet: position + 0.000001 >= minNotional,
      positionAtMost5USDT: position <= CFG.maxPosition + 0.000001,
      riskAtMost050USDT: riskFees <= CFG.maxRisk,
      netRewardRiskAtLeast2: netRR >= CFG.minNetRR,
    };

    const sourceRisk = assessSourceRisk(riskSources, summary.base, summary.symbol);
    checks.noOfficialBinanceRisk = !sourceRisk.binance.blocked;
    checks.noOnChainMarketRisk = !sourceRisk.cryptoQuant.blocked;
    checks.noLargeExchangeDepositRisk = !sourceRisk.whaleAlert.blocked;

    const valid = Object.values(checks).every(Boolean);
    const baseScore = valid
      ? netRR * 10 + Math.min(summary.volume / 1_000_000, 25) + trend.strength * 5 - spread * 8
      : 0;
    const score = Math.max(0, baseScore);

    return {
      symbol: summary.symbol,
      valid,
      status: valid ? "READY_SIGNAL" : "WAIT",
      setup: setup?.type || null,
      officialSources: sourceRisk,
      checks,
      market: {
        bid: fmt(bid), ask: fmt(ask), spreadPct: round(spread, 4),
        change24hPct: round(summary.change, 3), volume24hUSDT: round(summary.volume, 2),
        bidDepth1PctUSDT: round(depthSides.bid, 2), askDepth1PctUSDT: round(depthSides.ask, 2),
        atr15m: fmt(atr), stopDistancePct: round(stopPct, 2),
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
      score: round(score, 3),
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

function symbolTrend(candles) {
  const closes = candles.map(x => x.close);
  const e9 = ema(closes, 9), e20 = ema(closes, 20), e50 = ema(closes, 50);
  const last = candles.at(-1), prev = candles.at(-2);
  const rising = e9 > e20 && e20 > e50 && last.close > e20 && last.close >= prev.close * 0.995;
  return { rising, strength: (e9 - e20) / e20 };
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

function onePctDepth(depth, mid) {
  const bid = depth.bids.filter(([p]) => Number(p) >= mid * 0.99)
    .reduce((s, [p, q]) => s + Number(p) * Number(q), 0);
  const ask = depth.asks.filter(([p]) => Number(p) <= mid * 1.01)
    .reduce((s, [p, q]) => s + Number(p) * Number(q), 0);
  return { bid, ask };
}

async function binance(path) {
  let last = "unknown";
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
    volume: +k[5], closeTime: +k[6], quoteVolume: +k[7] };
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
    `🚨 أفضل إشارة Binance Spot: ${x.symbol}`,
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
    `حالة BTC: ${regime.state}`,
    `المصادر الرسمية: Binance آمن=${!x.officialSources.binance.blocked} | CryptoQuant آمن=${!x.officialSources.cryptoQuant.blocked} | Whale Alert آمن=${!x.officialSources.whaleAlert.blocked}`,
    `الإلغاء: ${p.invalidation}`,
    "⚠️ إشارة فقط وليست ضمان مكسب. البوت لا ينفذ أي أمر؛ راجعي Binance يدويًا قبل الدخول.",
  ].join("\n");
}

async function telegram(env, text) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) throw new Error("Telegram secrets missing");
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
  if (!r.ok) throw new Error(`Telegram error ${r.status}`);
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
