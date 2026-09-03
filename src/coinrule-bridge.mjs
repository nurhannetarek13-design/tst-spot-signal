/**
 * Coinrule Custom Signal bridge.
 *
 * Safety defaults:
 * - disabled unless COINRULE_BRIDGE_ENABLED=true
 * - secrets are read from environment variables only
 * - no Binance credentials are handled here
 * - designed to target a Coinrule Demo rule first
 *
 * Required env:
 *   COINRULE_SIGNAL_URL
 *   COINRULE_SIGNAL_MESSAGE_TEMPLATE
 *   COINRULE_BRIDGE_KEY
 *
 * Template placeholders:
 *   {{action}} -> buy | sell
 *   {{symbol}} -> e.g. BTCUSDT
 *
 * Example template copied/adapted from Coinrule:
 *   "{{action}} YOUR_COINRULE_TOKEN {{symbol}}"
 */

function env(name, fallback = "") {
  return String(process.env[name] ?? fallback).trim();
}

export function coinruleBridgeStatus() {
  return {
    enabled: env("COINRULE_BRIDGE_ENABLED", "false").toLowerCase() === "true",
    configured: Boolean(
      env("COINRULE_SIGNAL_URL") &&
      env("COINRULE_SIGNAL_MESSAGE_TEMPLATE") &&
      env("COINRULE_BRIDGE_KEY")
    ),
    mode: env("COINRULE_BRIDGE_MODE", "demo").toLowerCase(),
  };
}

export function verifyCoinruleBridgeKey(value) {
  const expected = env("COINRULE_BRIDGE_KEY");
  if (!expected || !value) return false;

  const a = Buffer.from(String(value));
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;

  // Constant-time comparison without pulling another dependency.
  return cryptoSafeEqual(a, b);
}

function cryptoSafeEqual(a, b) {
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a[i] ^ b[i];
  return diff === 0;
}

export function buildCoinruleMessage({ action, symbol }) {
  const normalizedAction = String(action || "").toLowerCase();
  const normalizedSymbol = String(symbol || "").toUpperCase();

  if (!["buy", "sell"].includes(normalizedAction)) {
    throw new Error("COINRULE_BAD_ACTION");
  }
  if (!/^[A-Z0-9]{2,20}USDT$/.test(normalizedSymbol)) {
    throw new Error("COINRULE_BAD_SYMBOL");
  }

  const template = env("COINRULE_SIGNAL_MESSAGE_TEMPLATE");
  if (!template) throw new Error("COINRULE_TEMPLATE_MISSING");

  return template
    .replaceAll("{{action}}", normalizedAction)
    .replaceAll("{{symbol}}", normalizedSymbol);
}

export async function sendCoinruleSignal({ action, symbol }) {
  const status = coinruleBridgeStatus();

  if (!status.enabled) {
    return { ok: false, status: "COINRULE_BRIDGE_DISABLED", mode: status.mode };
  }
  if (!status.configured) {
    return { ok: false, status: "COINRULE_BRIDGE_NOT_CONFIGURED", mode: status.mode };
  }
  if (status.mode !== "demo") {
    return {
      ok: false,
      status: "COINRULE_LIVE_BLOCKED",
      reason: "Bridge is intentionally locked to demo while being validated.",
      mode: status.mode,
    };
  }

  const url = env("COINRULE_SIGNAL_URL");
  const message = buildCoinruleMessage({ action, symbol });

  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "text/plain; charset=utf-8" },
    body: message,
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
  });

  const body = await response.text().catch(() => "");

  return {
    ok: response.ok,
    status: response.ok ? "COINRULE_SIGNAL_SENT" : "COINRULE_SIGNAL_REJECTED",
    httpStatus: response.status,
    mode: status.mode,
    response: body.slice(0, 500),
  };
}
