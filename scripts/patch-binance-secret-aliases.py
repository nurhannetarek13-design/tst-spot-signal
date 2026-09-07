from pathlib import Path

p = Path('src/buy-gateway.js')
s = p.read_text(encoding='utf-8')

old_creds = '''function creds(env) {
  return {
    key: env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || env.BINANCE_DEMO_API_KEY || "",
    secret: env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || env.BINANCE_DEMO_SECRET_KEY || "",
  };
}'''
new_creds = '''function creds(env) {
  const liveKey = env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "";
  const liveSecret = env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || "";
  if (liveKey && liveSecret) {
    return {
      key: String(liveKey).trim(),
      secret: String(liveSecret).trim(),
      network: "production",
      credentialMode: "LIVE",
    };
  }
  const demoKey = env.BINANCE_DEMO_API_KEY || "";
  const demoSecret = env.BINANCE_DEMO_SECRET_KEY || "";
  if (demoKey && demoSecret) {
    return {
      key: String(demoKey).trim(),
      secret: String(demoSecret).trim(),
      network: "testnet",
      credentialMode: "DEMO",
    };
  }
  return { key: "", secret: "", network: "none", credentialMode: "MISSING" };
}'''
if old_creds in s:
    s = s.replace(old_creds, new_creds, 1)
elif new_creds not in s:
    raise SystemExit('Binance creds helper changed unexpectedly')

old_destructure = '  const { key, secret } = creds(env);'
new_destructure = '  const { key, secret, network } = creds(env);'
if old_destructure in s:
    s = s.replace(old_destructure, new_destructure, 1)
elif new_destructure not in s:
    raise SystemExit('signed relay credential destructure changed unexpectedly')

old_body = '''    method: String(method).toUpperCase(),
    path,
    apiKey: key,
    query: `${qs}&signature=${binanceSignature}`,
'''
new_body = '''    method: String(method).toUpperCase(),
    path,
    apiKey: key,
    network,
    query: `${qs}&signature=${binanceSignature}`,
'''
if old_body in s:
    s = s.replace(old_body, new_body, 1)
elif new_body not in s:
    raise SystemExit('signed relay request body changed unexpectedly')

old_balance = '''      source: "CLOUDFLARE_SIGNED_VERCEL_TRANSPORT",
      checkedAt: Date.now(),
'''
new_balance = '''      source: "CLOUDFLARE_SIGNED_VERCEL_TRANSPORT",
      network: creds(env).network,
      credentialMode: creds(env).credentialMode,
      checkedAt: Date.now(),
'''
if old_balance in s:
    s = s.replace(old_balance, new_balance, 1)
elif new_balance not in s:
    raise SystemExit('balance metadata marker changed unexpectedly')

required = (
    'env.BINANCE_DEMO_API_KEY',
    'env.BINANCE_DEMO_SECRET_KEY',
    'network: "production"',
    'network: "testnet"',
    'credentialMode: "LIVE"',
    'credentialMode: "DEMO"',
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
print('verified Binance secret aliases; live routes to production, demo routes to testnet; no secret mutation performed')
