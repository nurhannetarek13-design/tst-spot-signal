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
    s, count = re.subn(
        r'requested\s*>\s*10',
        'requested > MAX_ORDER_USDT',
        s,
    )
    if 'MAX_ORDER_USDT' not in s:
        raise SystemExit(f'growth cap patch failed for {p}: MAX_ORDER constant missing')
    if re.search(r'requested\s*>\s*10', s):
        raise SystemExit(f'growth cap patch failed for {p}: legacy 10 USDT gate remains')
    if 'BAD_STAKE' in s and 'MAX_ORDER_USDT' not in s:
        raise SystemExit(f'growth cap patch failed for {p}: BAD_STAKE path not growth-aware')

    p.write_text(s, encoding='utf-8')
    print(f'[cloudflare-growth-cap] {p.name} OK replacements={count}')

# Final fail-closed repository check: no live gateway may retain the legacy 10 USDT gate.
for p in Path('src').glob('buy-gateway-*.js'):
    s = p.read_text(encoding='utf-8')
    if re.search(r'requested\s*>\s*10', s):
        raise SystemExit(f'growth cap patch failed: legacy 10 USDT gate still present in {p}')

print('[cloudflare-growth-cap] OK max accepted signal stake = 100 USDT across canonical + stable; actual sizing remains risk/balance controlled upstream')
