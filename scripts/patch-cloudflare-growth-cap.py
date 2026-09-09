from pathlib import Path
import re

FILES = [
    Path('src/buy-gateway-canonical.js'),
    Path('src/buy-gateway-stable.js'),
]

for p in FILES:
    s = p.read_text(encoding='utf-8')
    min_marker = 'const MIN_ORDER_USDT = 5;\n'
    if 'const MAX_ORDER_USDT = 100;' not in s:
        if min_marker not in s:
            raise SystemExit(f'growth cap patch failed for {p}: MIN_ORDER marker missing')
        s = s.replace(min_marker, min_marker + 'const MAX_ORDER_USDT = 100;\n', 1)

    # Accept growth-mode stakes consistently in every live signal ingest path.
    s, count = re.subn(r'requested\s*>\s*10', 'requested>MAX_ORDER_USDT', s)
    if 'MAX_ORDER_USDT' not in s:
        raise SystemExit(f'growth cap patch failed for {p}: MAX_ORDER constant missing')
    if re.search(r'requested\s*>\s*10', s):
        raise SystemExit(f'growth cap patch failed for {p}: legacy 10 USDT gate remains')
    p.write_text(s, encoding='utf-8')
    print(f'[cloudflare-growth-cap] {p.name} OK replacements={count}')

# Final fail-closed repository check: no live gateway may retain the legacy 10 USDT gate.
for p in Path('src').glob('buy-gateway-*.js'):
    s = p.read_text(encoding='utf-8')
    if re.search(r'requested\s*>\s*10', s):
        raise SystemExit(f'growth cap patch failed: legacy 10 USDT gate still present in {p}')

# Add a non-trading runtime probe at the outer auth wrapper. It creates a valid
# internal HMAC request with dryRun=true and proves the deployed downstream
# worker actually accepts a 40 USDT signal. No order can be placed by this probe.
auth = Path('src/buy-gateway-auth-wrapper.js')
a = auth.read_text(encoding='utf-8')
probe_marker = 'async function readBinanceAccountRelay(request, env) {'
probe_fn = '''async function growthCapDryRun(env, ctx) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    return Response.json({ ok:false, status:"TELEGRAM_NOT_CONFIGURED" }, { status:503 });
  }
  const body = JSON.stringify({
    id: `growth-cap-${Date.now()}`,
    symbol: "BTCUSDT",
    entry: 100,
    stop: 99,
    target: 102,
    stakeUSDT: 40,
    score: 100,
    strategy: "GROWTH_CAP_RUNTIME_PROBE",
    dryRun: true,
  });
  const ts = String(Date.now());
  const sig = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
  const req = new Request("https://internal/fast-signal-ingest", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-fast-timestamp": ts,
      "x-fast-signature": sig,
      "cache-control": "no-store",
    },
    body,
  });
  const r = await worker.fetch(req, env, ctx);
  const text = await r.text();
  let row = {}; try { row = JSON.parse(text || "{}"); } catch { row = { ok:false, status:"NON_JSON" }; }
  return Response.json({
    ok: r.ok && row.ok === true && row.status === "FAST_SIGNAL_DRYRUN_OK" && Number(row.recommendedUSDT) === 40,
    status: row.status || `HTTP_${r.status}`,
    recommendedUSDT: row.recommendedUSDT ?? null,
    testedStakeUSDT: 40,
    dryRun: true,
    autoBuy: false,
    noSecretValuesExposed: true,
  }, { status: r.ok ? 200 : r.status, headers: { "cache-control": "no-store" } });
}

'''
if 'async function growthCapDryRun(env, ctx)' not in a:
    if probe_marker not in a:
        raise SystemExit('growth cap runtime probe failed: insertion marker missing')
    a = a.replace(probe_marker, probe_fn + probe_marker, 1)

route_marker = '    const url = new URL(request.url);\n'
route_line = '    if (url.pathname === "/growth-cap-check" && request.method === "GET") return growthCapDryRun(env, ctx);\n'
if '/growth-cap-check' not in a:
    if route_marker not in a:
        raise SystemExit('growth cap runtime probe failed: route marker missing')
    a = a.replace(route_marker, route_marker + route_line, 1)

auth.write_text(a, encoding='utf-8')
print('[cloudflare-growth-cap] runtime dry-run probe installed at /growth-cap-check')
print('[cloudflare-growth-cap] OK max accepted signal stake = 100 USDT across canonical + stable; actual sizing remains risk/balance controlled upstream')
