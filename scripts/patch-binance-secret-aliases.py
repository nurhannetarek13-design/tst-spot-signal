from pathlib import Path

p = Path('src/buy-gateway.js')
s = p.read_text(encoding='utf-8')

old = '''function creds(env) {
  return {
    key: env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || env.BINANCE_DEMO_API_KEY || "",
    secret: env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || env.BINANCE_DEMO_SECRET_KEY || "",
  };
}'''
new = '''function creds(env) {
  const key = env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || env.BINANCE_DEMO_API_KEY || "";
  const secret = env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || env.BINANCE_DEMO_SECRET_KEY || "";
  return { key: String(key).trim(), secret: String(secret).trim() };
}'''
if old in s:
    s = s.replace(old, new, 1)
elif new not in s:
    raise SystemExit('Binance creds helper changed unexpectedly')

required = (
    'env.BINANCE_DEMO_API_KEY',
    'env.BINANCE_DEMO_SECRET_KEY',
    'demoApiKeyBindingPresent',
    'demoSecretBindingPresent',
    'noSecretValuesExposed',
)
missing = [item for item in required if item not in s]
if missing:
    raise SystemExit(f'Missing required existing-secret runtime guards: {missing}')
if 'CLOUDFLARE_SIGNED_VERCEL_TRANSPORT' not in s:
    raise SystemExit('Expected Cloudflare-signed Vercel transport route is missing')
if 'autoBuy: false' not in s and 'autoBuy:false' not in s:
    raise SystemExit('autoBuy=false guard is missing')

p.write_text(s, encoding='utf-8')
print('verified existing Binance secret aliases; normalized whitespace only; no secret mutation performed')
