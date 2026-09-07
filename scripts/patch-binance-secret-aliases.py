from pathlib import Path

p = Path('src/buy-gateway.js')
s = p.read_text(encoding='utf-8')

old_creds = 'function creds(env){return{key:env.BINANCE_API_KEY||env.BINANCE_KEY||env.BINANCE_APIKEY||"",secret:env.BINANCE_API_SECRET||env.BINANCE_SECRET||env.BINANCE_SECRET_KEY||""};}'
new_creds = 'function creds(env){return{key:env.BINANCE_API_KEY||env.BINANCE_KEY||env.BINANCE_APIKEY||env.BINANCE_DEMO_API_KEY||"",secret:env.BINANCE_API_SECRET||env.BINANCE_SECRET||env.BINANCE_SECRET_KEY||env.BINANCE_DEMO_SECRET_KEY||""};}'

if old_creds in s:
    s = s.replace(old_creds, new_creds, 1)
elif new_creds not in s:
    raise SystemExit('Binance creds helper changed; refusing unsafe patch')

old_key_alias = 'keyAlias:env.BINANCE_API_KEY?"BINANCE_API_KEY":env.BINANCE_KEY?"BINANCE_KEY":env.BINANCE_APIKEY?"BINANCE_APIKEY":null'
new_key_alias = 'keyAlias:env.BINANCE_API_KEY?"BINANCE_API_KEY":env.BINANCE_KEY?"BINANCE_KEY":env.BINANCE_APIKEY?"BINANCE_APIKEY":env.BINANCE_DEMO_API_KEY?"BINANCE_DEMO_API_KEY":null'
if old_key_alias in s:
    s = s.replace(old_key_alias, new_key_alias, 1)
elif new_key_alias not in s:
    raise SystemExit('Runtime key alias probe changed; refusing unsafe patch')

old_secret_alias = 'secretAlias:env.BINANCE_API_SECRET?"BINANCE_API_SECRET":env.BINANCE_SECRET?"BINANCE_SECRET":env.BINANCE_SECRET_KEY?"BINANCE_SECRET_KEY":null'
new_secret_alias = 'secretAlias:env.BINANCE_API_SECRET?"BINANCE_API_SECRET":env.BINANCE_SECRET?"BINANCE_SECRET":env.BINANCE_SECRET_KEY?"BINANCE_SECRET_KEY":env.BINANCE_DEMO_SECRET_KEY?"BINANCE_DEMO_SECRET_KEY":null'
if old_secret_alias in s:
    s = s.replace(old_secret_alias, new_secret_alias, 1)
elif new_secret_alias not in s:
    raise SystemExit('Runtime secret alias probe changed; refusing unsafe patch')

for required in ('env.BINANCE_DEMO_API_KEY', 'env.BINANCE_DEMO_SECRET_KEY'):
    if required not in s:
        raise SystemExit(f'Missing required existing secret alias: {required}')

p.write_text(s, encoding='utf-8')
print('existing Binance Cloudflare secret aliases enabled and runtime-check aware')
