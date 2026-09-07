from pathlib import Path

s = Path('src/buy-gateway.js').read_text(encoding='utf-8')

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

print('verified existing Binance secret aliases; no secret mutation performed')
