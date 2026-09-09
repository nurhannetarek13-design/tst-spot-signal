from pathlib import Path
import re

# Runtime contract v4: Cloudflare must accept growth-mode dry-runs up to 100 USDT.
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

    s, count = re.subn(r'requested\s*>\s*10', 'requested>MAX_ORDER_USDT', s)
    if 'MAX_ORDER_USDT' not in s:
        raise SystemExit(f'growth cap patch failed for {p}: MAX_ORDER constant missing')
    if re.search(r'requested\s*>\s*10', s):
        raise SystemExit(f'growth cap patch failed for {p}: legacy 10 USDT gate remains')
    p.write_text(s, encoding='utf-8')
    print(f'[cloudflare-growth-cap] {p.name} OK replacements={count}')

for p in Path('src').glob('buy-gateway-*.js'):
    s = p.read_text(encoding='utf-8')
    if re.search(r'requested\s*>\s*10', s):
        raise SystemExit(f'growth cap patch failed: legacy 10 USDT gate still present in {p}')

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

# Insert the route specifically inside the outer auth wrapper fetch() before fast-signal handling.
fetch_marker = '''  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") {
'''
fetch_replacement = '''  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/growth-cap-check" && request.method === "GET") {
      return growthCapDryRun(env, ctx);
    }
    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") {
'''
if 'url.pathname === "/growth-cap-check"' not in a:
    if fetch_marker not in a:
        raise SystemExit('growth cap runtime probe failed: outer fetch marker missing')
    a = a.replace(fetch_marker, fetch_replacement, 1)

auth.write_text(a, encoding='utf-8')
print('[cloudflare-growth-cap] runtime dry-run probe installed at outer /growth-cap-check')
print('[cloudflare-growth-cap] OK max accepted signal stake = 100 USDT across canonical + stable')
