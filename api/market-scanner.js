const API_BASES = ['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com'];

// Stable/fiat-like assets are intentionally excluded from momentum discovery.
// They can show volume/order-flow scores without having the directional range
// this bot is designed to trade (UUSDT was the concrete false positive).
const EXCLUDED_BASES = new Set([
  'U','USDC','FDUSD','TUSD','USDP','DAI','BUSD','USD1','RLUSD',
  'USDE','USDS','PYUSD','AEUR','EURI','EUR'
]);

async function binance(path) {
  let last;
  for (const base of API_BASES) {
    try {
      const r = await fetch(base + path, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(10000) });
      if (!r.ok) { last = `HTTP ${r.status}`; continue; }
      return await r.json();
    } catch (e) { last = e.message; }
  }
  throw new Error(`Binance unavailable: ${last}`);
}

export default async function handler(req, res) {
  try {
    const [info, tickers] = await Promise.all([binance('/api/v3/exchangeInfo'), binance('/api/v3/ticker/24hr')]);
    const symbols = new Map(
      info.symbols
        .filter(s => s.status === 'TRADING' && s.quoteAsset === 'USDT' && s.isSpotTradingAllowed && !EXCLUDED_BASES.has(s.baseAsset))
        .map(s => [s.symbol, s])
    );
    const markets = tickers.filter(t => symbols.has(t.symbol)).map(t => ({ symbol: t.symbol, volume: +t.quoteVolume, change: +t.priceChangePercent, last: +t.lastPrice })).sort((a,b) => b.volume-a.volume);
    const movers = [...markets].sort((a,b) => b.change-a.change).slice(0,30);
    const liquid = markets.slice(0,120);
    return res.status(200).json({ ok:true, checkedAt:new Date().toISOString(), totalSpotUsdt:markets.length, liquid:liquid.map(x=>x.symbol), movers:movers.slice(0,20) });
  } catch (e) {
    return res.status(503).json({ ok:false, error:e.message });
  }
}