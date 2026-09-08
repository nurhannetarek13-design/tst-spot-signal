// Legacy direct-execution endpoint intentionally disabled.
// Production execution is allowed only through:
// Telegram CONFIRM -> Cloudflare durable one-time lock -> signed Railway relay -> Make -> Binance Spot.
//
// Keep this route fail-closed so an old client, bookmark, or deployment cannot bypass
// the canonical confirmation path.

function json(res, status, body) {
  res.status(status).json(body);
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return json(res, 405, {
      ok: false,
      status: "METHOD_NOT_ALLOWED",
      autoBuy: false,
    });
  }

  return json(res, 410, {
    ok: false,
    status: "LEGACY_DIRECT_EXECUTION_DISABLED",
    executionRoute: "TELEGRAM_CONFIRM_CLOUDFLARE_MAKE_ONLY",
    autoBuy: false,
    userConfirmationRequired: true,
  });
}
