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
        headers: { 'User-Agent': 'tst-vercel-market-scan/1.0', accept: 'application/json' },
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

async function sendTelegram(text) {
  const token = process.env.TELEGRAM_BOT_TOKEN || '';
  const chatId = process.env.TELEGRAM_CHAT_ID || '';
  if (!token || !chatId) return { sent: false, reason: 'telegram_env_missing' };
  const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
    signal: AbortSignal.timeout(10000)
  });
  if (!r.ok) return { sent: false, reason: `telegram_http_${r.status}` };
  return { sent: true };
}

export default async function handler(req, res) {
  try {
    const secret = process.env.SCANNER_TRIGGER_SECRET || '';
    if (secret && req.headers.authorization !== `Bearer ${secret}`) {
      return res.status(401).json({ ok: false, error: 'unauthorized' });
    }

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
        changePct: Number(t.priceChangePercent || 0),
        quoteVolume: Number(t.quoteVolume || 0),
        lastPrice: Number(t.lastPrice || 0),
        tradeCount: Number(t.count || 0)
      }));

    const liquidMovers = rows
      .filter(x => x.quoteVolume >= 1_000_000)
      .sort((a,b) => Math.abs(b.changePct) - Math.abs(a.changePct))
      .slice(0, 25);

    const momentum = rows
      .filter(x => x.quoteVolume >= 2_000_000 && x.changePct > 0)
      .sort((a,b) => (b.changePct * Math.log10(Math.max(b.quoteVolume,10))) - (a.changePct * Math.log10(Math.max(a.quoteVolume,10))))
      .slice(0, 50);

    const newestCandidates = rows
      .filter(x => x.quoteVolume < 5_000_000)
      .sort((a,b) => b.tradeCount - a.tradeCount)
      .slice(0, 50);

    const shouldAlert = req.query?.alert === '1';
    let telegram = null;
    if (shouldAlert && liquidMovers.length) {
      const top = liquidMovers.slice(0, 8).map(x => `${x.symbol} ${x.changePct >= 0 ? '+' : ''}${x.changePct.toFixed(1)}%`).join('\n');
      telegram = await sendTelegram(`🌐 ALL-MARKET SCAN\nTracking ${rows.length} Binance Spot/USDT markets\n\nTop movers:\n${top}`);
    }

    return res.status(200).json({
      ok: true,
      checkedAt: new Date().toISOString(),
      marketCount: rows.length,
      liquidMovers,
      momentum,
      newestCandidates,
      telegram
    });
  } catch (error) {
    return res.status(503).json({ ok: false, error: error.message });
  }
}
