from pathlib import Path

p = Path("src/buy-gateway-stable.js")
s = p.read_text(encoding="utf-8")

old_constants = '''const VERCEL_SIGNED_RELAY_URL = "https://tst-spot-signal.vercel.app/api/binance-signed-relay";
const RAILWAY_DEMO_ACCOUNT_URL = "https://liquidation-collector-production.up.railway.app/signed-testnet-account";
const DEMO_ROUTE = "CLOUDFLARE_SIGNED_RAILWAY_TESTNET_READONLY";
const LIVE_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_TRANSPORT";'''
new_constants = '''const VERCEL_SIGNED_RELAY_URL = "https://tst-spot-signal.vercel.app/api/binance-signed-relay";
const DEMO_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_DEMO_READONLY";
const LIVE_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_TRANSPORT";'''
if old_constants not in s and new_constants not in s:
    raise SystemExit("stable gateway constants changed unexpectedly")
s = s.replace(old_constants, new_constants)

s = s.replace('''      network: "testnet",
      credentialMode: "DEMO",
      route: DEMO_ROUTE,''', '''      network: "demo",
      credentialMode: "DEMO",
      route: DEMO_ROUTE,''')

old_demo = '''  if (c.credentialMode === "DEMO") {
    if (String(method).toUpperCase() !== "GET" || path !== "/api/v3/account") {
      throw new Error("DEMO_EXECUTION_DISABLED");
    }
    const r = await fetch(RAILWAY_DEMO_ACCOUNT_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "cache-control": "no-store" },
      body: JSON.stringify({ apiKey: c.key, query: signedQuery }),
    });
    const text = await r.text();
    let row = {};
    try { row = JSON.parse(text || "{}"); } catch { row = { ok: false, status: "NON_JSON_RAILWAY_RESPONSE" }; }
    if (!r.ok || row.ok !== true) {
      const detail = row?.upstream?.code != null
        ? `${row.upstream.code} ${row.upstream.msg || ""}`
        : (row.reason || row.status || r.status);
      throw new Error(`BINANCE_RAILWAY_DEMO_ERROR: ${detail}`);
    }
    return row.data;
  }

  if (c.credentialMode !== "LIVE") throw new Error("LIVE_CREDENTIALS_REQUIRED");
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("RELAY_SECRET_UNAVAILABLE");

  const body = JSON.stringify({
    method: String(method).toUpperCase(),
    path,
    apiKey: c.key,
    network: "production",
    query: signedQuery,
  });'''
new_demo = '''  if (c.credentialMode === "DEMO" && (String(method).toUpperCase() !== "GET" || path !== "/api/v3/account")) {
    throw new Error("DEMO_EXECUTION_DISABLED");
  }
  if (c.credentialMode !== "LIVE" && c.credentialMode !== "DEMO") throw new Error("LIVE_CREDENTIALS_REQUIRED");
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("RELAY_SECRET_UNAVAILABLE");

  const relayNetwork = c.credentialMode === "DEMO" ? "demo" : "production";
  const body = JSON.stringify({
    method: String(method).toUpperCase(),
    path,
    apiKey: c.key,
    network: relayNetwork,
    query: signedQuery,
  });'''
if old_demo not in s and new_demo not in s:
    raise SystemExit("stable gateway demo block changed unexpectedly")
s = s.replace(old_demo, new_demo)

required = [
    'const DEMO_ROUTE = "CLOUDFLARE_SIGNED_VERCEL_DEMO_READONLY";',
    'network: "demo"',
    'const relayNetwork = c.credentialMode === "DEMO" ? "demo" : "production";',
    'throw new Error("DEMO_EXECUTION_DISABLED")',
    'VERCEL_SIGNED_RELAY_URL',
    'autoBuy: false',
]
for marker in required:
    if marker not in s:
        raise SystemExit(f"required marker missing: {marker}")

for forbidden in [
    "RAILWAY_DEMO_ACCOUNT_URL",
    "liquidation-collector-production.up.railway.app/signed-testnet-account",
    "BINANCE_RAILWAY_DEMO_ERROR",
]:
    if forbidden in s:
        raise SystemExit(f"forbidden stale demo route remains: {forbidden}")

p.write_text(s, encoding="utf-8")
print("stable demo account route materialized through Vercel; execution remains disabled for DEMO credentials")
