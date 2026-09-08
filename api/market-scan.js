const API_BASES = [
  'https://data-api.binance.vision',
  'https://api.binance.com',
  'https://api1.binance.com',
  'https://api2.binance.com',
  'https://api3.binance.com'
];

async function binance(path) {
  let last = 'unknown';
  for (const base of API_BASES) {
    try {
      const r = await fetch(base + path, {
        headers: { 'User-Agent': 'tst-vercel-market-scan/1.1', accept: 'application/json' },
        signal: AbortSignal.timeout(12000)
      });
      if (!r.ok) { last = `${base}:${r.status}`; continue; }
      return await r.json();
    } catch (e) { last = e.message; }
  }
  throw new Error(`Binance unavailable: ${last}`);
}

function isExcludedBase(base) {
  const stable = new Set(['USDC','FDUSD','TUSD','USDP','DAI','EUR','AEUR','TRY','BRL','BUSD']);
  return stable.has(base) || /(UP|DOWN|BULL|BEAR)$/.test(base);
}

async function collect() {
  const [info, tickers] = await Promise.all([
    binance('/api/v3/exchangeInfo'),
    binance('/api/v3/ticker/24hr')
  ]);

  const tradable = new Map(
    info.symbols
      .filter(s => s.quoteAsset === 'USDT' && s.status === 'TRADING' && s.isSpotTradingAllowed && !isExcludedBase(s.baseAsset))
      .map(s => [s.symbol, s])
  );

  const rows = tickers
    .filter(t => tradable.has(t.symbol))
    .map(t => ({
      symbol: t.symbol,
      pair: `${tradable.get(t.symbol).baseAsset}/USDT`,
      changePct: Number(t.priceChangePercent || 0),
      quoteVolume: Number(t.quoteVolume || 0),
      lastPrice: Number(t.lastPrice || 0),
      tradeCount: Number(t.count || 0)
    }));

  const liquid = [...rows].sort((a,b) => b.quoteVolume - a.quoteVolume);
  const momentum = rows
    .filter(x => x.quoteVolume >= 2_000_000 && x.changePct > 0)
    .sort((a,b) => (b.changePct * Math.log10(Math.max(b.quoteVolume,10))) - (a.changePct * Math.log10(Math.max(a.quoteVolume,10))));

  const selected = [...new Map([
    ...momentum.slice(0, 80),
    ...liquid.slice(0, 120)
  ].map(x => [x.symbol, x])).values()].slice(0, 120);

  return { rows, selected, liquidMovers: [...rows].filter(x => x.quoteVolume >= 1_000_000).sort((a,b) => Math.abs(b.changePct)-Math.abs(a.changePct)).slice(0,25), momentum: momentum.slice(0,50) };
}

export default async function handler(req, res) {
  try {
    const data = await collect();
    if (req.query?.format === 'freqtrade') {
      return res.status(200).json({ pairs: data.selected.map(x => x.pair), refresh_period: 60 });
    }
    return res.status(200).json({
      ok: true,
      checkedAt: new Date().toISOString(),
      marketCount: data.rows.length,
      selectedCount: data.selected.length,
      selected: data.selected.map(x => x.pair),
      liquidMovers: data.liquidMovers,
      momentum: data.momentum
    });
  } catch (error) {
    return res.status(503).json({ ok: false, error: error.message });
  }
}
