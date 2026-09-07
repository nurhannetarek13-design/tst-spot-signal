from pathlib import Path

p = Path('src/buy-gateway.js')
s = p.read_text(encoding='utf-8')

old = 'function creds(env){return{key:env.BINANCE_API_KEY||env.BINANCE_KEY||env.BINANCE_APIKEY||"",secret:env.BINANCE_API_SECRET||env.BINANCE_SECRET||env.BINANCE_SECRET_KEY||""};}'
new = 'function creds(env){return{key:env.BINANCE_API_KEY||env.BINANCE_KEY||env.BINANCE_APIKEY||env.BINANCE_DEMO_API_KEY||"",secret:env.BINANCE_API_SECRET||env.BINANCE_SECRET||env.BINANCE_SECRET_KEY||env.BINANCE_DEMO_SECRET_KEY||""};}'

if old in s:
    s = s.replace(old, new, 1)
elif new not in s:
    raise SystemExit('Binance creds helper changed; refusing unsafe patch')

p.write_text(s, encoding='utf-8')
print('existing Binance Cloudflare secret aliases enabled')
