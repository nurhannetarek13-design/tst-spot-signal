from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Keep this guard deliberately simple and robust: the normal engine already
# rejects anything in EXCLUDE, so add every dollar/fiat-like asset there.
# This works even after the new-listing reservation patch changes symbol_ok().
# Railway watches this file directly so any future stablecoin additions redeploy.
extra = [
    'UUSDT',
    'USDEUSDT',
    'USDSUSDT',
    'PYUSDUSDT',
    'EURIUSDT',
]

marker = 'EXCLUDE = {\n'
if marker not in s:
    raise SystemExit('stablecoin patch failed: EXCLUDE marker missing')

missing = [sym for sym in extra if f"'{sym}'" not in s]
if missing:
    injected = "    " + ', '.join(repr(x) for x in missing) + ',\n'
    s = s.replace(marker, marker + injected, 1)

path.write_text(s, encoding='utf-8')

# Build-time hard assertions: the exact false positive that triggered this patch
# must never reach the momentum engine again, while normal alts remain eligible.
ns = {}
exec(compile(s, str(path), 'exec'), ns)
for sym in ['UUSDT', 'USDCUSDT', 'FDUSDUSDT', 'USD1USDT', 'RLUSDUSDT', 'USDEUSDT', 'USDSUSDT', 'PYUSDUSDT', 'EURIUSDT']:
    assert ns['symbol_ok'](sym) is False, sym
for sym in ['DOGEUSDT', 'SOLUSDT']:
    assert ns['symbol_ok'](sym) is True, sym
print('[stablecoin-exclusion] OK U/stablecoins blocked; DOGE/SOL remain eligible')
