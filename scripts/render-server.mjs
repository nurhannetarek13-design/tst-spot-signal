import crypto from "node:crypto";
import http from "node:http";

const PORT = Number(process.env.PORT || 10000);
const WORKER_URL = process.env.WORKER_URL || "https://tst-spot-signal.nurhanne-tarek13.workers.dev/";
const RUN_TOKEN = process.env.RUN_TOKEN || "";
const RELAY_SECRET = process.env.RELAY_SECRET || "";
const API_BASES = [
  "https://api.binance.com", "https://api-gcp.binance.com", "https://api1.binance.com",
  "https://api2.binance.com", "https://api3.binance.com", "https://api4.binance.com",
  "https://data-api.binance.vision",
];
const CFG = { maxPosition: 5, maxRisk: 0.5, fee: 0.001, minVolume: 2_000_000,
  minDepth: 10_000, maxSpreadPct: 0.5, maxStopPct: 8, minNetRR: 2, scanCount: 18 };
const EXCLUDED = new Set(["BTC", "ETH", "BNB", "SOL", "USDC", "FDUSD", "TUSD", "USDP", "DAI",
  "EUR", "AEUR", "TRY", "BRL", "BIDR", "IDRT", "UAH", "NGN", "RUB", "GBP", "AUD", "BUSD"]);

async function binance(path) {
  let last = "unknown";
  for (const base of API_BASES) {
    try {
      const response = await fetch(base + path, { headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-render/1.0" }, signal: AbortSignal.timeout(15_000) });
      if (!response.ok) { last = `${base}: HTTP ${response.status}`; continue; }
      const data = await response.json();
      if (data?.code) { last = `${base}: ${data.code} ${data.msg || ""}`; continue; }
      return data;
    } catch (error) { last = `${base}: ${error.message}`; }
  }
  throw new Error(`All Binance public endpoints failed (${last})`);
}

function candle(k) { return { openTime: +k[0], open: +k[1], high: +k[2], low: +k[3], close: +k[4], closeTime: +k[6], quoteVolume: +k[7] }; }
function closed(rows) { return rows.map(candle).filter(x => x.closeTime < Date.now()); }
function ema(values, period) { const a = 2 / (period + 1); let out = values[0]; for (let i = 1; i < values.length; i++) out = values[i] * a + out * (1 - a); return out; }
function median(values) { const s = [...values].sort((a, b) => a - b), m = Math.floor(s.length / 2); return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
function atr14(rows) { const s = rows.slice(-15), tr = []; for (let i = 1; i < s.length; i++) tr.push(Math.max(s[i].high - s[i].low, Math.abs(s[i].high - s[i - 1].close), Math.abs(s[i].low - s[i - 1].close))); return tr.reduce((a, b) => a + b, 0) / Math.max(1, tr.length); }
function floorStep(value, step) { const p = Math.max(0, Math.ceil(-Math.log10(step))); return Number((Math.floor(value / step) * step).toFixed(p)); }
function round(value, digits = 4) { const p = 10 ** digits; return Math.round(value * p) / p; }
function fmt(value) { return Number(value).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 }); }

function marketRegime(h1, h4) {
  const v1 = h1.map(x => x.close), v4 = h4.map(x => x.close);
  const e20h1 = ema(v1, 20), e50h1 = ema(v1, 50), e50h4 = ema(v4, 50);
  const allowed = h1.at(-1).close > e50h1 && h4.at(-1).close > e50h4 && e20h1 >= e50h1 * 0.995;
  return { allowed, state: allowed ? "LONGS_ALLOWED" : "RISK_OFF" };
}
function risingTrend(rows) { const v = rows.map(x => x.close), e9 = ema(v, 9), e20 = ema(v, 20), e50 = ema(v, 50); return e9 > e20 && e20 > e50 && rows.at(-1).close > e20 && rows.at(-1).close >= rows.at(-2).close * 0.995; }
function breakoutRetest(rows) {
  const start = Math.max(25, rows.length - 16);
  for (let i = start; i < rows.length - 1; i++) {
    const prior = rows.slice(i - 20, i), resistance = Math.max(...prior.map(x => x.high)), breakout = rows[i];
    if (breakout.close <= resistance * 1.001 || breakout.quoteVolume < median(prior.map(x => x.quoteVolume)) * 1.2) continue;
    for (let j = i + 1; j < rows.length; j++) {
      const retest = rows[j]; if (retest.close < resistance * 0.99) break;
      if (retest.low <= resistance * 1.006 && retest.close >= resistance * 0.998 && rows.at(-1).close >= resistance && rows.at(-1).close <= resistance * 1.08) return { type: "BREAKOUT_RETEST", retest, time: retest.openTime };
    }
  }
  return null;
}
function tstSetup(rows, ask) {
  const recent = rows.slice(-24);
  for (let i = 0; i < recent.length - 1; i++) {
    if (recent[i].close <= 0.01855) continue;
    for (let j = i + 1; j < recent.length; j++) {
      const retest = recent[j];
      if (retest.low <= 0.01855 && retest.high >= 0.01848 && retest.close >= 0.01848 && ask >= 0.01856 && ask <= 0.01865) return { type: "TST_EXACT_BREAKOUT_RETEST", retest, time: retest.openTime };
      if (retest.close < 0.01848) break;
    }
  }
  return null;
}
function depthWithinOnePct(depth, mid) {
  return { bid: depth.bids.filter(([p]) => +p >= mid * 0.99).reduce((s, [p, q]) => s + +p * +q, 0), ask: depth.asks.filter(([p]) => +p <= mid * 1.01).reduce((s, [p, q]) => s + +p * +q, 0) };
}

async function analyze(summary, info, book) {
  try {
    const [raw15, raw1h, depth] = await Promise.all([binance(`/api/v3/klines?symbol=${summary.symbol}&interval=15m&limit=120`), binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1h&limit=100`), binance(`/api/v3/depth?symbol=${summary.symbol}&limit=100`)]);
    const c15 = closed(raw15), c1h = closed(raw1h), bid = +book.bidPrice, ask = +book.askPrice, mid = (bid + ask) / 2;
    const spread = ((ask - bid) / mid) * 100, liquidity = depthWithinOnePct(depth, mid);
    const setup = summary.symbol === "TSTUSDT" ? tstSetup(c15, ask) : breakoutRetest(c15);
    const atr = atr14(c15), stop = Math.min((setup?.retest.low || ask) * 0.998, ask - 1.15 * atr), stopPct = stop > 0 && stop < ask ? ((ask - stop) / ask) * 100 : 999, unitRisk = ask - stop;
    const lot = info.filters.find(x => x.filterType === "LOT_SIZE"), notional = info.filters.find(x => x.filterType === "NOTIONAL" || x.filterType === "MIN_NOTIONAL");
    const quantity = floorStep(Math.min(CFG.maxPosition / ask, CFG.maxRisk / Math.max(unitRisk, 1e-12)), +(lot?.stepSize || 1e-8));
    const position = quantity * ask, risk = quantity * unitRisk + position * CFG.fee + quantity * stop * CFG.fee, target1 = ask + 2.5 * unitRisk, target2 = ask + 3.2 * unitRisk;
    const reward = quantity * (target1 - ask) - position * CFG.fee - quantity * target1 * CFG.fee, rr = risk > 0 ? reward / risk : 0;
    const checks = { volume: summary.volume >= CFG.minVolume, change: summary.change >= -8 && summary.change <= 25, trend: risingTrend(c1h), setup: Boolean(setup), spread: spread < CFG.maxSpreadPct, bidDepth: liquidity.bid >= CFG.minDepth, askDepth: liquidity.ask >= CFG.minDepth, stop: stopPct > 0 && stopPct <= CFG.maxStopPct, minOrder: position >= +(notional?.minNotional || 5), position: position <= CFG.maxPosition + 0.0001, risk: risk <= CFG.maxRisk + 0.0001, rr: rr >= CFG.minNetRR };
    const valid = Object.values(checks).every(Boolean);
    return { symbol: summary.symbol, valid, checks, setup, score: valid ? rr * 10 + Math.log10(summary.volume) - spread : 0, market: { spread, change: summary.change, volume: summary.volume }, plan: valid ? { entry: ask, stop, target1, target2, quantity, position, risk, rr } : null };
  } catch (error) { return { symbol: summary.symbol, valid: false, error: error.message }; }
}

async function scan() {
  const [tickers, books, exchange, btc1h, btc4h] = await Promise.all([binance("/api/v3/ticker/24hr"), binance("/api/v3/ticker/bookTicker"), binance("/api/v3/exchangeInfo"), binance("/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"), binance("/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=100")]);
  const regime = marketRegime(closed(btc1h), closed(btc4h));
  if (!regime.allowed) return { checkedAt: new Date().toISOString(), status: "MARKET_RISK_OFF", regime, candidates: [], results: [], best: null };
  const tradable = new Map(exchange.symbols.filter(x => x.status === "TRADING" && x.quoteAsset === "USDT" && x.isSpotTradingAllowed && !EXCLUDED.has(x.baseAsset) && !/UP$|DOWN$|BULL$|BEAR$/.test(x.baseAsset)).map(x => [x.symbol, x])), bookMap = new Map(books.map(x => [x.symbol, x]));
  const eligible = tickers.map(x => ({ symbol: x.symbol, volume: +x.quoteVolume, change: +x.priceChangePercent })).filter(x => tradable.has(x.symbol) && bookMap.has(x.symbol) && x.volume >= CFG.minVolume && x.change >= -8 && x.change <= 25);
  const liquid = [...eligible].sort((a, b) => b.volume - a.volume).slice(0, 10), active = [...eligible].filter(x => x.change > 0).sort((a, b) => b.change * Math.log10(b.volume) - a.change * Math.log10(a.volume)).slice(0, 12), priority = eligible.filter(x => x.symbol === "TSTUSDT");
  const candidates = [...new Map([...priority, ...liquid, ...active].map(x => [x.symbol, x])).values()].slice(0, CFG.scanCount), results = [];
  for (let i = 0; i < candidates.length; i += 4) results.push(...await Promise.all(candidates.slice(i, i + 4).map(x => analyze(x, tradable.get(x.symbol), bookMap.get(x.symbol)))));
  const best = results.filter(x => x.valid).sort((a, b) => b.score - a.score)[0] || null;
  return { checkedAt: new Date().toISOString(), status: best ? "VALID_SETUP_FOUND" : "NO_VALID_SETUP", regime, candidates: candidates.map(x => x.symbol), results, best };
}

function alertText(best) {
  const p = best.plan, m = best.market;
  return [`🚨 إشارة Binance Spot مؤكدة: ${best.symbol}`, `النوع: ${best.setup.type}`, `الدخول الورقي: ${fmt(p.entry)}`, `الكمية: ${fmt(p.quantity)} — قيمة الصفقة: ${round(p.position, 4)} USDT`, `وقف الخسارة: ${fmt(p.stop)}`, `الهدف الأول: ${fmt(p.target1)} | الهدف الثاني: ${fmt(p.target2)}`, `المخاطرة شاملة الرسوم: ${round(p.risk, 4)} USDT | R:R: ${round(p.rr, 2)}`, `السبريد: ${round(m.spread, 4)}% | حجم 24س: ${round(m.volume, 2)} USDT`, "⚠️ تنبيه ورقي فقط — لا يوجد شراء حقيقي أو رافعة مالية."].join("\n");
}
async function relayTelegram(text) {
  if (!RELAY_SECRET) throw new Error("RELAY_SECRET is not configured");
  const body = JSON.stringify({ timestamp: Date.now(), text }), signature = crypto.createHmac("sha256", RELAY_SECRET).update(body).digest("hex");
  const response = await fetch(new URL("?relay=telegram", WORKER_URL), { method: "POST", headers: { "Content-Type": "application/json", "X-Relay-Signature": signature }, body });
  const raw = await response.text(); if (!response.ok) throw new Error(`Telegram relay HTTP ${response.status}: ${raw.slice(0, 200)}`); return JSON.parse(raw);
}
function authorized(request) { return Boolean(RUN_TOKEN) && request.headers.authorization === `Bearer ${RUN_TOKEN}`; }
function send(response, status, data) { response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" }); response.end(JSON.stringify(data)); }

http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  try {
    if (url.pathname === "/") return send(response, 200, { ok: true, service: "tst-spot-signal", mode: "SIGNAL_ONLY", liveTrading: false });
    if (url.pathname === "/health") { const time = await binance("/api/v3/time"); return send(response, 200, { ok: true, binance: "reachable", serverTime: time.serverTime, region: process.env.RENDER_REGION || "unknown" }); }
    if (url.pathname === "/scan" && request.method === "POST") { if (!authorized(request)) return send(response, 401, { ok: false, error: "Unauthorized" }); const report = await scan(); let telegram = null; if (report.best) telegram = await relayTelegram(alertText(report.best)); return send(response, 200, { ok: true, mode: "SIGNAL_ONLY", liveTrading: false, alertSent: Boolean(report.best && telegram), report }); }
    if (url.pathname === "/test-telegram" && request.method === "POST") { if (!authorized(request)) return send(response, 401, { ok: false, error: "Unauthorized" }); const telegram = await relayTelegram("✅ Render متصل ببوت TST Signal — الفحص الورقي أصبح جاهزًا، بدون أي تداول حقيقي."); return send(response, 200, { ok: true, telegram }); }
    return send(response, 404, { ok: false, error: "Not found" });
  } catch (error) { return send(response, 503, { ok: false, error: error.message, liveTrading: false }); }
}).listen(PORT, "0.0.0.0", () => console.log(`tst-spot-signal listening on ${PORT}`));
