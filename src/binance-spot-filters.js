function decimals(step) {
  const s = String(step || "1").toLowerCase();
  if (s.includes("e-")) return Number(s.split("e-")[1] || 0);
  const i = s.indexOf(".");
  return i < 0 ? 0 : s.slice(i + 1).replace(/0+$/, "").length;
}

function normalizeDecimalInput(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) throw new Error("NON_FINITE_DECIMAL");
  if (!/[eE]/.test(String(value))) return String(value);
  return n.toFixed(20).replace(/0+$/, "").replace(/.$/, "");
}

function scaledInt(value, scale) {
  const raw = normalizeDecimalInput(value);
  const neg = raw.startsWith("-");
  const body = neg ? raw.slice(1) : raw;
  const [wholeRaw, fracRaw = ""] = body.split(".");
  const whole = wholeRaw || "0";
  const frac = (fracRaw + "0".repeat(scale)).slice(0, scale);
  const out = BigInt(whole) * (10n ** BigInt(scale)) + BigInt(frac || "0");
  return neg ? -out : out;
}

function formatScaledInt(value, scale) {
  const neg = value < 0n;
  let raw = (neg ? -value : value).toString().padStart(scale + 1, "0");
  if (scale === 0) return (neg ? "-" : "") + raw;
  const whole = raw.slice(0, -scale) || "0";
  const frac = raw.slice(-scale).replace(/0+$/, "");
  return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
}

export function floorToFilterStep(value, step) {
  const scale = Math.max(decimals(step), decimals(value));
  const v = scaledInt(value, scale);
  const st = scaledInt(step, scale);
  if (v <= 0n || st <= 0n) return "0";
  return formatScaledInt((v / st) * st, scale);
}

function floorToPrecision(value, precision = 8) {
  const p = Math.max(0, Math.min(20, Number(precision) || 0));
  const v = scaledInt(value, p);
  return formatScaledInt(v, p);
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
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

async function fetchPublic(path, publicFetcher = null) {
  if (typeof publicFetcher === "function") {
    const data = await publicFetcher(path);
    if (!data || typeof data !== "object") throw new Error("BINANCE_PUBLIC_PROXY_BAD_RESPONSE");
    return data;
  }
  let last = null;
  for (const base of EXCHANGE_INFO_BASES) {
    try {
      const r = await fetch(base + path, {
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
  throw new Error("BINANCE_PUBLIC_UNAVAILABLE:" + String(last || "all-hosts-failed"));
}

async function fetchExchangeInfo(symbol, publicFetcher = null) {
  return fetchPublic("/api/v3/exchangeInfo?symbol=" + encodeURIComponent(symbol), publicFetcher);
}

function filterMap(row) {
  return Object.fromEntries((row?.filters || []).map((f) => [f.filterType, f]));
}

function effectiveMarketLot(filters) {
  const market = filters.MARKET_LOT_SIZE || {};
  const lot = filters.LOT_SIZE || {};
  const marketStep = num(market.stepSize);
  const marketMin = num(market.minQty);
  const marketMax = num(market.maxQty);
  return {
    stepSize: marketStep > 0 ? market.stepSize : (lot.stepSize || "1"),
    minQty: marketMin > 0 ? market.minQty : (lot.minQty || "0"),
    maxQty: marketMax > 0 ? market.maxQty : (lot.maxQty || "0"),
    source: marketStep > 0 || marketMin > 0 || marketMax > 0 ? "MARKET_LOT_SIZE" : "LOT_SIZE",
  };
}

function activeMinNotional(filters) {
  const notional = filters.NOTIONAL || null;
  const min = filters.MIN_NOTIONAL || null;
  if (notional && notional.applyMinToMarket !== false) return num(notional.minNotional);
  if (min && min.applyToMarket !== false) return num(min.minNotional);
  return 0;
}

export async function validateSpotMarketBuy(symbol, quoteUSDT, availableQuoteBalance, { referencePrice = null, publicFetcher = null } = {}) {
  const s = String(symbol || "").toUpperCase();
  if (!/^[A-Z0-9]{2,20}USDT$/.test(s)) throw new Error("BAD_SPOT_SYMBOL");

  const [j, book] = await Promise.all([
    fetchExchangeInfo(s, publicFetcher),
    fetchPublic("/api/v3/ticker/bookTicker?symbol=" + encodeURIComponent(s), publicFetcher),
  ]);
  const row = Array.isArray(j?.symbols) ? j.symbols[0] : null;
  if (!row) throw new Error("SYMBOL_INFO_MISSING");
  if (row.status !== "TRADING") throw new Error("SYMBOL_NOT_TRADING");
  if (row.isSpotTradingAllowed !== true) throw new Error("SPOT_TRADING_NOT_ALLOWED");
  if (String(row.quoteAsset || "") !== "USDT") throw new Error("QUOTE_ASSET_NOT_USDT");
  if (!Array.isArray(row.orderTypes) || !row.orderTypes.includes("MARKET")) throw new Error("MARKET_ORDER_NOT_ALLOWED");
  if (row.quoteOrderQtyMarketAllowed === false) throw new Error("QUOTE_ORDER_QTY_MARKET_NOT_ALLOWED");

  const quote = num(quoteUSDT);
  const free = num(availableQuoteBalance);
  if (!(quote > 0)) throw new Error("BAD_QUOTE_AMOUNT");
  if (!(free >= quote)) throw new Error("INSUFFICIENT_QUOTE_BALANCE");

  const ask = num(book?.askPrice);
  const bid = num(book?.bidPrice);
  if (!(ask > 0 && bid > 0 && ask >= bid)) throw new Error("BOOK_PRICE_UNAVAILABLE");
  if (referencePrice != null && num(referencePrice) > 0) {
    const deviation = ask / num(referencePrice) - 1;
    if (deviation > 0.0035) throw new Error("STALE_PRICE_CHASE");
  }

  const filters = filterMap(row);
  const lot = filters.LOT_SIZE || {};
  const marketLot = effectiveMarketLot(filters);
  const priceFilter = filters.PRICE_FILTER || {};
  const minNotional = activeMinNotional(filters);
  const quotePrecision = Number(row.quoteAssetPrecision ?? row.quotePrecision ?? 8);
  const quoteOrderQty = floorToPrecision(quote, quotePrecision);
  const estimatedBaseRaw = quote / ask;
  const estimatedBaseQty = floorToFilterStep(estimatedBaseRaw, marketLot.stepSize || "1");

  const estimatedQtyNum = num(estimatedBaseQty);
  const minQty = num(marketLot.minQty);
  const maxQty = num(marketLot.maxQty);
  if (!(estimatedQtyNum > 0)) throw new Error("ESTIMATED_MARKET_QTY_ZERO");
  if (minQty > 0 && estimatedQtyNum < minQty) throw new Error("MARKET_QTY_BELOW_MIN");
  if (maxQty > 0 && estimatedQtyNum > maxQty) throw new Error("MARKET_QTY_ABOVE_MAX");
  if (minNotional > 0 && num(quoteOrderQty) < minNotional) throw new Error("MARKET_NOTIONAL_BELOW_MIN");

  return {
    ok: true,
    symbol: s,
    quoteOrderQty,
    quoteAsset: String(row.quoteAsset || ""),
    baseAsset: String(row.baseAsset || ""),
    askPrice: String(book.askPrice || ""),
    bidPrice: String(book.bidPrice || ""),
    estimatedBaseQty,
    symbolStatus: row.status,
    spotTradingAllowed: row.isSpotTradingAllowed === true,
    marketOrderAllowed: true,
    quoteOrderQtyMarketAllowed: row.quoteOrderQtyMarketAllowed !== false,
    quantityPrecision: Number(row.baseAssetPrecision ?? row.baseCommissionPrecision ?? 8),
    pricePrecision: Number(row.quotePrecision ?? row.quoteAssetPrecision ?? 8),
    filters: {
      lotSize: {
        minQty: lot.minQty || null,
        maxQty: lot.maxQty || null,
        stepSize: lot.stepSize || null,
      },
      marketLotSize: {
        minQty: marketLot.minQty || null,
        maxQty: marketLot.maxQty || null,
        stepSize: marketLot.stepSize || null,
        source: marketLot.source,
      },
      priceFilter: {
        minPrice: priceFilter.minPrice || null,
        maxPrice: priceFilter.maxPrice || null,
        tickSize: priceFilter.tickSize || null,
      },
      minNotional,
    },
  };
}

export async function normalizeSpotProtection(symbol, quantity, takeProfit, stopLoss, stopLimit, { publicFetcher = null } = {}) {
  const s = String(symbol || "").toUpperCase();
  const j = await fetchExchangeInfo(s, publicFetcher);
  const row = Array.isArray(j?.symbols) ? j.symbols[0] : null;
  if (!row || row.status !== "TRADING") throw new Error("SYMBOL_NOT_TRADING");
  if (row.isSpotTradingAllowed !== true) throw new Error("SPOT_TRADING_NOT_ALLOWED");

  const byType = filterMap(row);
  const lot = byType.LOT_SIZE || {};
  const price = byType.PRICE_FILTER || {};
  const notional = byType.NOTIONAL || byType.MIN_NOTIONAL || {};
  const qty = floorToFilterStep(quantity, lot.stepSize || "1");
  const tp = floorToFilterStep(takeProfit, price.tickSize || "0.00000001");
  const sl = floorToFilterStep(stopLoss, price.tickSize || "0.00000001");
  const slLimit = floorToFilterStep(stopLimit, price.tickSize || "0.00000001");
  const minQty = num(lot.minQty);
  const maxQty = num(lot.maxQty);
  const minNotional = num(notional.minNotional);

  if (!(num(qty) > 0) || num(qty) < minQty) throw new Error("NORMALIZED_QTY_BELOW_MIN");
  if (maxQty > 0 && num(qty) > maxQty) throw new Error("NORMALIZED_QTY_ABOVE_MAX");
  if (!(num(tp) > num(sl) && num(sl) > num(slLimit) && num(slLimit) > 0)) throw new Error("INVALID_OCO_PRICE_RELATIONSHIP");
  if (minNotional > 0 && num(qty) * num(slLimit) < minNotional) throw new Error("PROTECTION_NOTIONAL_BELOW_MIN");

  return {
    quantity: qty,
    takeProfit: tp,
    stopLoss: sl,
    stopLimit: slLimit,
    minNotional,
    stepSize: lot.stepSize || null,
    tickSize: price.tickSize || null,
  };
}
