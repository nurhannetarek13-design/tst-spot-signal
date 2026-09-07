import crypto from "node:crypto";

const QUOTE_USDT = 5.5;
const MAX_SIGNAL_AGE_MS = 10 * 60 * 1000;
const API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function json(res, status, body) {
  res.status(status).json(body);
}

function timingSafeHexEqual(a, b) {
  if (!/^[a-f0-9]{64}$/i.test(a || "") || !/^[a-f0-9]{64}$/i.test(b || "")) return false;
  const aa = Buffer.from(a, "hex");
  const bb = Buffer.from(b, "hex");
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function verifyRelay(req, raw) {
  const secret = process.env.TELEGRAM_BOT_TOKEN;
  if (!secret) return { ok: false, reason: "relay secret unavailable" };
  const ts = String(req.headers["x-executor-timestamp"] || "");
  const signature = String(req.headers["x-executor-signature"] || "").toLowerCase();
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) return { ok: false, reason: "stale relay timestamp" };
  const expected = crypto.createHmac("sha256", secret).update(`${ts}.${raw}`).digest("hex");
  return timingSafeHexEqual(signature, expected) ? { ok: true } : { ok: false, reason: "bad relay signature" };
}

async function publicBinance(path) {
  const bases = ["https://data-api.binance.vision", ...API_BASES];
  let last = "unavailable";
  for (const base of bases) {
    try {
      const r = await fetch(base + path);
      const text = await r.text();
      if (r.ok) return JSON.parse(text || "{}");
      last = `${r.status} ${text}`;
    } catch (e) { last = String(e?.message || e); }
  }
  throw new Error(`Binance public API failed: ${last}`);
}

async function signedBinance(method, path, params = {}) {
  const apiKey = process.env.BINANCE_API_KEY;
  const secret = process.env.BINANCE_API_SECRET;
  if (!apiKey || !secret) throw new Error("Binance API signing keys are missing in Vercel");
  const all = { ...params, recvWindow: 5000, timestamp: Date.now() };
  const qs = new URLSearchParams(Object.entries(all).map(([k, v]) => [k, String(v)])).toString();
  const signature = crypto.createHmac("sha256", secret).update(qs).digest("hex");
  let last = "unknown";
  for (const base of API_BASES) {
    try {
      const r = await fetch(`${base}${path}?${qs}&signature=${signature}`, {
        method,
        headers: { "X-MBX-APIKEY": apiKey, "content-type": "application/x-www-form-urlencoded" },
      });
      const text = await r.text();
      const data = JSON.parse(text || "{}");
      if (r.ok && !(Number(data.code) < 0)) return data;
      last = `${data.code || r.status} ${data.msg || text}`;
    } catch (e) { last = String(e?.message || e); }
  }
  throw new Error(`Binance signed request failed: ${last}`);
}

function decimals(step) {
  const s = String(step);
  if (s.includes("e-")) return Number(s.split("e-")[1]);
  return (s.split(".")[1] || "").replace(/0+$/, "").length;
}
function floorTo(value, step) {
  if (!step || step <= 0) return value;
  return Number((Math.floor((value + 1e-12) / step) * step).toFixed(decimals(step)));
}
function roundTo(value, tick) {
  if (!tick || tick <= 0) return value;
  return Number((Math.round(value / tick) * tick).toFixed(decimals(tick)));
}

export default async function handler(req, res) {
  if (req.method !== "POST") return json(res, 405, { ok: false, status: "METHOD_NOT_ALLOWED" });
  try {
    const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    const auth = verifyRelay(req, raw);
    if (!auth.ok) return json(res, 401, { ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const body = JSON.parse(raw || "{}");
    if (body.userConfirmed !== true) return json(res, 409, { ok: false, status: "USER_CONFIRMATION_REQUIRED" });

    const symbol = String(body.symbol || "").toUpperCase();
    if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) return json(res, 400, { ok: false, status: "BAD_SYMBOL" });
    const createdAt = Number(body.createdAt || 0);
    if (!Number.isFinite(createdAt) || Date.now() - createdAt > MAX_SIGNAL_AGE_MS || createdAt > Date.now() + 30_000) {
      return json(res, 409, { ok: false, status: "STALE_SIGNAL" });
    }

    const entryRef = Number(body.entry);
    const stopRef = Number(body.stop);
    const targetRef = Number(body.target);
    if (![entryRef, stopRef, targetRef].every(Number.isFinite) || entryRef <= 0 || stopRef <= 0 || targetRef <= 0) {
      return json(res, 400, { ok: false, status: "BAD_RISK_LEVELS" });
    }
    if (!(stopRef < entryRef && targetRef > entryRef)) return json(res, 400, { ok: false, status: "INVALID_TP_SL_GEOMETRY" });

    const info = await publicBinance(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`);
    const market = info.symbols?.[0];
    if (!market || market.status !== "TRADING" || !market.isSpotTradingAllowed || market.quoteAsset !== "USDT") {
      return json(res, 409, { ok: false, status: "PAIR_NOT_TRADABLE_SPOT" });
    }

    const account = await signedBinance("GET", "/api/v3/account", {});
    if (!account.canTrade) return json(res, 409, { ok: false, status: "ACCOUNT_CANNOT_TRADE" });
    const freeUSDT = Number((account.balances || []).find((b) => b.asset === "USDT")?.free || 0);
    if (freeUSDT < QUOTE_USDT) return json(res, 409, { ok: false, status: "INSUFFICIENT_USDT", freeUSDT });

    const buy = await signedBinance("POST", "/api/v3/order", {
      symbol,
      side: "BUY",
      type: "MARKET",
      quoteOrderQty: QUOTE_USDT.toFixed(2),
      newOrderRespType: "FULL",
      newClientOrderId: `TSTU${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`,
    });

    const executedQty = Number(buy.executedQty || 0);
    const quoteQty = Number(buy.cummulativeQuoteQty || 0);
    if (!(executedQty > 0 && quoteQty > 0)) throw new Error("Market BUY returned zero fill");
    const avg = quoteQty / executedQty;

    const lot = market.filters.find((x) => x.filterType === "LOT_SIZE");
    const pf = market.filters.find((x) => x.filterType === "PRICE_FILTER");
    const step = Number(lot?.stepSize || "0.00000001");
    const tick = Number(pf?.tickSize || "0.00000001");
    const stop = roundTo(avg * (stopRef / entryRef), tick);
    const tp = roundTo(avg * (targetRef / entryRef), tick);
    const stopLimit = roundTo(stop * 0.997, tick);
    const sellQty = floorTo(executedQty * 0.999, step);

    let oco = null;
    let ocoError = null;
    try {
      oco = await signedBinance("POST", "/api/v3/orderList/oco", {
        symbol,
        side: "SELL",
        quantity: sellQty,
        aboveType: "LIMIT_MAKER",
        abovePrice: tp,
        belowType: "STOP_LOSS_LIMIT",
        belowStopPrice: stop,
        belowPrice: stopLimit,
        belowTimeInForce: "GTC",
      });
    } catch (e) {
      ocoError = String(e?.message || e);
    }

    return json(res, 200, {
      ok: true,
      status: oco ? "BOUGHT_AND_PROTECTED" : "BOUGHT_PROTECTION_FAILED",
      symbol,
      quoteUSDT: quoteQty,
      executedQty,
      avg,
      tp,
      stop,
      stopLimit,
      ocoPlaced: Boolean(oco),
      ocoError,
      autoBuy: false,
      userConfirmed: true,
    });
  } catch (e) {
    return json(res, 500, { ok: false, status: "EXECUTION_ERROR", reason: String(e?.message || e) });
  }
}
