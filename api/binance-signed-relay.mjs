// Legacy Vercel Binance relay is intentionally disabled.
// Canonical execution path is Telegram Confirm -> Cloudflare -> Railway signed relay -> Make -> Binance.
// Keeping this endpoint fail-closed prevents stale callers from generating Binance signature/502 errors.

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  return res.status(200).json({
    ok: false,
    status: 'LEGACY_BINANCE_RELAY_DISABLED',
    canonicalExecution: 'MAKE',
    tradingAction: 'NONE'
  });
}
