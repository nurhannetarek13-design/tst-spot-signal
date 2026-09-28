function decimals(step) {
  const s = String(step || "1").toLowerCase();
  if (s.includes("e-")) return Number(s.split("e-")[1] || 0);
  const i = s.indexOf(".");
  return i < 0 ? 0 : s.slice(i + 1).replace(/0+$/, "").length;
}
function floorStep(value, step) {
  const n = Number(value), st = Number(step);
  if (!(n > 0) || !(st > 0)) return 0;
  const d = decimals(step);
  const units = Math.floor((n + st * 1e-9) / st);
  return Number((units * st).toFixed(d));
}
const EXCHANGE_INFO_BASES = [
  "https://data-api.binance.vision",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://api.binance.com",
];

async function fetchExchangeInfo(symbol) {
  let last = null;
  for (const base of EXCHANGE_INFO_BASES) {
    try {
      const r = await fetch(base + "/api/v3/exchangeInfo?symbol=" + encodeURIComponent(symbol), {
        headers: { "cache-control": "no-store", "accept": "application/json" },
        signal: AbortSignal.timeout(8_000),
      });
      if (r.ok) return await r.json();
      const text = typeof r.text === "function" ? await r.text() : "";
      last = String(r.status) + ":" + String(text || "").slice(0,120);
    } catch (error) {
      last = String(error?.message || error).slice(0,120);
    }
  }
  throw new Error("EXCHANGE_INFO_UNAVAILABLE:" + String(last || "all-hosts-failed"));
}

export async function normalizeSpotProtection(symbol, quantity, takeProfit, stopLoss, stopLimit) {
  const s = String(symbol || "").toUpperCase();
  const j = await fetchExchangeInfo(s);
  const row = Array.isArray(j?.symbols) ? j.symbols[0] : null;
  if (!row || row.status !== "TRADING") throw new Error("SYMBOL_NOT_TRADING");
  const byType = Object.fromEntries((row.filters || []).map((f) => [f.filterType, f]));
  const lot = byType.LOT_SIZE || {};
  const price = byType.PRICE_FILTER || {};
  const notional = byType.NOTIONAL || byType.MIN_NOTIONAL || {};
  const qty = floorStep(quantity, lot.stepSize || "1");
  const tp = floorStep(takeProfit, price.tickSize || "0.00000001");
  const sl = floorStep(stopLoss, price.tickSize || "0.00000001");
  const slLimit = floorStep(stopLimit, price.tickSize || "0.00000001");
  const minQty = Number(lot.minQty || 0);
  const minNotional = Number(notional.minNotional || 0);
  if (!(qty > 0) || qty < minQty) throw new Error("NORMALIZED_QTY_BELOW_MIN");
  if (!(tp > sl && sl > slLimit && slLimit > 0)) throw new Error("INVALID_OCO_PRICE_RELATIONSHIP");
  if (minNotional > 0 && qty * slLimit < minNotional) throw new Error("PROTECTION_NOTIONAL_BELOW_MIN");
  return { quantity: qty, takeProfit: tp, stopLoss: sl, stopLimit: slLimit, minNotional, stepSize: lot.stepSize || null, tickSize: price.tickSize || null };
}
