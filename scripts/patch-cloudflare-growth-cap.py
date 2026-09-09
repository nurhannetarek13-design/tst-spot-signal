from pathlib import Path

p = Path('src/buy-gateway-canonical.js')
s = p.read_text(encoding='utf-8')

min_marker = 'const MIN_ORDER_USDT = 5;\n'
if 'const MAX_ORDER_USDT = 100;' not in s:
    if min_marker not in s:
        raise SystemExit('growth cap patch failed: MIN_ORDER marker missing')
    s = s.replace(min_marker, min_marker + 'const MAX_ORDER_USDT = 100;\n', 1)

old = 'requested<MIN_ORDER_USDT || requested>10'
new = 'requested<MIN_ORDER_USDT || requested>MAX_ORDER_USDT'
if old in s:
    s = s.replace(old, new, 1)
elif new not in s:
    raise SystemExit('growth cap patch failed: stake validation marker missing')

p.write_text(s, encoding='utf-8')
print('[cloudflare-growth-cap] OK max accepted signal stake = 100 USDT; actual sizing remains risk/balance controlled upstream')
