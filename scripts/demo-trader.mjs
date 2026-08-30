import crypto from "node:crypto";

const API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://data-api.binance.vision",
];
const DEMO_BASE = "https://demo-api.binance.com";
const WORKER_URL = "https://tst-spot-signal.nurhanne-tarek13.workers.dev/";
const API_KEY = process.env.BINANCE_DEMO_API_KEY;
const SECRET_KEY = process.env.BINANCE_DEMO_SECRET_KEY;
const FEE = 0.001;
const MAX_POSITION = 5;
const MAX_RISK = 0.5;
const MIN_VOLUME = 2_000_000;
const EXCLUDED = new Set(["BTC", "ETH", "BNB", "SOL", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "TRY", "BRL", "BIDR", "IDRT", "UAH", "NGN", "RUB", "GBP", "AUD", "BUSD"]);

async function binance(path) {
  let last = "unknown";
  for (const base of API_BASES) {
    try {
      const response = await fetch(base + path, { headers: { Accept: "application/json", "User-Agent": "Mozilla/5.0 local-spot-scanner" } });
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
function fmt(value) { return Number(value).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 }); }
function round(value, digits = 4) { const p = 10 ** digits; return Math.round(value * p) / p; }

function risingTrend(rows) {
  const values = rows.map(x => x.close);
  const e9 = ema(values, 9), e20 = ema(values, 20), e50 = ema(values, 50);
  return e9 > e20 && e20 > e50 && rows.at(-1).close > e20 && rows.at(-1).close >= rows.at(-2).close * 0.995;
}

function breakoutRetest(rows) {
  const start = Math.max(25, rows.length - 16);
  for (let i = start; i < rows.length - 1; i++) {
    const prior = rows.slice(i - 20, i), resistance = Math.max(...prior.map(x => x.high));
    const b = rows[i];
    if (b.close <= resistance * 1.001 || b.quoteVolume < median(prior.map(x => x.quoteVolume)) * 1.2) continue;
    for (let j = i + 1; j < rows.length; j++) {
      const r = rows[j];
      if (r.close < resistance * 0.99) break;
      if (r.low <= resistance * 1.006 && r.close >= resistance * 0.998 && rows.at(-1).close >= resistance && rows.at(-1).close <= resistance * 1.08) {
        return { type: "BREAKOUT_RETEST", level: resistance, retest: r, time: r.openTime };
      }
    }
  }
  return null;
}

function tstSetup(rows, ask) {
  const recent = rows.slice(-24);
  for (let i = 0; i < recent.length - 1; i++) {
    if (recent[i].close <= 0.01855) continue;
    for (let j = i + 1; j < recent.length; j++) {
      const r = recent[j];
      if (r.low <= 0.01855 && r.high >= 0.01848 && r.close >= 0.01848 && ask >= 0.01856 && ask <= 0.01865) {
        return { type: "TST_EXACT_BREAKOUT_RETEST", level: 0.01855, retest: r, time: r.openTime };
      }
      if (r.close < 0.01848) break;
    }
  }
  return null;
}

function depthWithinOnePct(depth, mid) {
  const bid = depth.bids.filter(([p]) => +p >= mid * 0.99).reduce((s, [p, q]) => s + (+p * +q), 0);
  const ask = depth.asks.filter(([p]) => +p <= mid * 1.01).reduce((s, [p, q]) => s + (+p * +q), 0);
  return { bid, ask };
}

async function analyze(summary, info, book) {
  try {
    const [raw15, raw1h, depth] = await Promise.all([
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=15m&limit=120`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1h&limit=100`),
      binance(`/api/v3/depth?symbol=${summary.symbol}&limit=100`),
    ]);
    const c15 = closed(raw15), c1h = closed(raw1h);
    const bid = +book.bidPrice, ask = +book.askPrice, mid = (bid + ask) / 2;
    const spread = ((ask - bid) / mid) * 100, liquidity = depthWithinOnePct(depth, mid);
    const setup = summary.symbol === "TSTUSDT" ? tstSetup(c15, ask) : breakoutRetest(c15);
    const atr = atr14(c15), stop = Math.min(setup?.retest.low * 0.998 || ask, ask - 1.15 * atr);
    const stopPct = stop > 0 && stop < ask ? ((ask - stop) / ask) * 100 : 999;
    const unitRisk = ask - stop;
    const lot = info.filters.find(x => x.filterType === "LOT_SIZE");
    const notional = info.filters.find(x => x.filterType === "NOTIONAL" || x.filterType === "MIN_NOTIONAL");
    const quantity = floorStep(Math.min(MAX_POSITION / ask, MAX_RISK / Math.max(unitRisk, 1e-12)), +(lot?.stepSize || 1e-8));
    const position = quantity * ask, risk = quantity * unitRisk + position * FEE + quantity * stop * FEE;
    const target1 = ask + 2.5 * unitRisk, target2 = ask + 3.2 * unitRisk;
    const reward = quantity * (target1 - ask) - position * FEE - quantity * target1 * FEE;
    const rr = risk > 0 ? reward / risk : 0;
    const checks = {
      volume: summary.volume >= MIN_VOLUME,
      change: summary.change <= 25,
      trend: risingTrend(c1h),
      setup: Boolean(setup),
      spread: spread < 0.5,
      bidDepth: liquidity.bid >= 10_000,
      askDepth: liquidity.ask >= 10_000,
      stop: stopPct > 0 && stopPct <= 8,
      minOrder: position >= +(notional?.minNotional || 5),
      position: position <= MAX_POSITION + 0.0001,
      risk: risk <= MAX_RISK + 0.0001,
      rr: rr >= 2,
    };
    const valid = Object.values(checks).every(Boolean);
    return { symbol: summary.symbol, valid, checks, setup, score: valid ? rr * 10 + Math.log10(summary.volume) - spread : 0,
      market: { ask, spread, change: summary.change, volume: summary.volume, liquidity, atr, stopPct },
      plan: valid ? { entry: ask, stop, target1, target2, quantity, position, risk, rr } : null };
  } catch (error) { return { symbol: summary.symbol, valid: false, error: error.message }; }
}

async function scan() {
  const [tickers, books, exchange] = await Promise.all([binance("/api/v3/ticker/24hr"), binance("/api/v3/ticker/bookTicker"), binance("/api/v3/exchangeInfo")]);
  const tradable = new Map(exchange.symbols.filter(x => x.status === "TRADING" && x.quoteAsset === "USDT" && x.isSpotTradingAllowed && !EXCLUDED.has(x.baseAsset) && !/UP$|DOWN$|BULL$|BEAR$/.test(x.baseAsset)).map(x => [x.symbol, x]));
  const bookMap = new Map(books.map(x => [x.symbol, x]));
  const eligible = tickers.map(x => ({ symbol: x.symbol, volume: +x.quoteVolume, change: +x.priceChangePercent })).filter(x => tradable.has(x.symbol) && bookMap.has(x.symbol) && x.volume >= MIN_VOLUME && x.change >= -8 && x.change <= 25);
  const liquid = [...eligible].sort((a, b) => b.volume - a.volume).slice(0, 10);
  const active = [...eligible].filter(x => x.change > 0).sort((a, b) => (b.change * Math.log10(b.volume)) - (a.change * Math.log10(a.volume))).slice(0, 12);
  const priority = eligible.filter(x => x.symbol === "TSTUSDT");
  const candidates = [...new Map([...priority, ...liquid, ...active].map(x => [x.symbol, x])).values()].slice(0, 18);
  const results = [];
  for (let i = 0; i < candidates.length; i += 4) results.push(...await Promise.all(candidates.slice(i, i + 4).map(x => analyze(x, tradable.get(x.symbol), bookMap.get(x.symbol)))));
  return { checkedAt: new Date().toISOString(), candidates: candidates.map(x => x.symbol), results, best: results.filter(x => x.valid).sort((a, b) => b.score - a.score)[0] || null };
}

async function relayTelegram(text) {
  if (!SECRET_KEY) throw new Error("Missing relay signing secret");
  const body = JSON.stringify({ timestamp: Date.now(), text });
  const signature = crypto.createHmac("sha256", SECRET_KEY).update(body).digest("hex");
  const response = await fetch(new URL("?relay=telegram", WORKER_URL), { method: "POST", headers: { "Content-Type": "application/json", "X-Relay-Signature": signature }, body });
  const raw = await response.text();
  if (!response.ok) throw new Error(`Telegram relay HTTP ${response.status}: ${raw.slice(0, 200)}`);
  return raw;
}

function clean(value) { return Number(value).toFixed(12).replace(/0+$/, "").replace(/\.$/, ""); }
async function signed(method, path, params = {}) {
  if (!API_KEY || !SECRET_KEY) throw new Error("Missing Binance Demo secrets");
  const query = new URLSearchParams({ ...params, recvWindow: "10000", timestamp: String(Date.now()) });
  query.set("signature", crypto.createHmac("sha256", SECRET_KEY).update(query.toString()).digest("hex"));
  const response = await fetch(method === "GET" ? `${DEMO_BASE}${path}?${query}` : `${DEMO_BASE}${path}`, { method, headers: { "X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" }, body: method === "GET" ? undefined : query.toString() });
  const raw = await response.text(); let data;
  try { data = JSON.parse(raw); } catch { throw new Error(`Binance Demo HTTP ${response.status}: returned HTML`); }
  if (!response.ok || data.code) throw new Error(`Binance Demo ${data.code || response.status}: ${data.msg || "request failed"}`);
  return data;
}

async function placeDemo(best) {
  const account = await signed("GET", "/api/v3/account");
  if (!account.canTrade) throw new Error("Demo trading permission disabled");
  const open = await signed("GET", "/api/v3/openOrders", { symbol: best.symbol });
  if (open.length) return { placed: false, reason: "existing open order" };
  const id = `gh${crypto.createHash("sha256").update(`${best.symbol}|${best.setup.time}`).digest("hex").slice(0, 20)}`;
  const history = await signed("GET", "/api/v3/allOrders", { symbol: best.symbol, limit: "50" });
  if (history.some(x => x.clientOrderId === id)) return { placed: false, reason: "signal already traded" };
  const info = await binance(`/api/v3/exchangeInfo?symbol=${best.symbol}`), filters = info.symbols[0].filters;
  const lot = filters.find(x => x.filterType === "LOT_SIZE"), priceFilter = filters.find(x => x.filterType === "PRICE_FILTER");
  const qty = floorStep(best.plan.quantity, +lot.stepSize), protectedQty = floorStep(qty * 0.999, +lot.stepSize);
  const price = value => floorStep(value, +priceFilter.tickSize);
  const order = await signed("POST", "/api/v3/orderList/otoco", { symbol: best.symbol, workingType: "LIMIT", workingSide: "BUY", workingPrice: clean(price(best.plan.entry)), workingQuantity: clean(qty), workingTimeInForce: "GTC", workingClientOrderId: id, pendingSide: "SELL", pendingQuantity: clean(protectedQty), pendingAboveType: "LIMIT_MAKER", pendingAbovePrice: clean(price(best.plan.target1)), pendingBelowType: "STOP_LOSS", pendingBelowStopPrice: clean(price(best.plan.stop)), newOrderRespType: "RESULT" });
  return { placed: true, id: order.orderListId };
}

function alertText(best, demo) {
  const p = best.plan, m = best.market;
  return [`🚨 إشارة Binance Spot مؤكدة: ${best.symbol}`, `النوع: ${best.setup.type}`, `الدخول: ${fmt(p.entry)}`, `الكمية: ${fmt(p.quantity)} — قيمة الصفقة: ${round(p.position, 4)} USDT`, `وقف الخسارة: ${fmt(p.stop)}`, `الهدف الأول: ${fmt(p.target1)}`, `الهدف الثاني: ${fmt(p.target2)}`, `المخاطرة شاملة الرسوم: ${round(p.risk, 4)} USDT`, `R:R الصافي: ${round(p.rr, 2)}`, `السبريد: ${round(m.spread, 4)}% | حجم 24س: ${round(m.volume, 2)} USDT`, demo?.placed ? `✅ تم وضع أمر تجريبي #${demo.id} — أموال وهمية فقط` : `ℹ️ الأمر التجريبي: ${demo?.reason || demo?.error || "لم يوضع"}`, `⚠️ لا يوجد أي تداول حقيقي أو رافعة مالية.`].join("\n");
}

async function main() {
  const report = await scan();
  console.log(JSON.stringify({ checkedAt: report.checkedAt, candidates: report.candidates, valid: report.results.filter(x => x.valid).map(x => x.symbol), errors: report.results.filter(x => x.error) }, null, 2));
  if (!report.best) { console.log("No valid setup. No alert and no order."); return; }
  let demo;
  try { demo = await placeDemo(report.best); } catch (error) { demo = { placed: false, error: error.message }; }
  await relayTelegram(alertText(report.best, demo));
  console.log("Telegram alert sent.");
}

main().catch(error => { console.error(error?.stack || error); process.exitCode = 1; });
