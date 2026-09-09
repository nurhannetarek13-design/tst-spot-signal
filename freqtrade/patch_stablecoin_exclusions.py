from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

stable_block = """STABLE_BASES = {\n    'U', 'USDC', 'FDUSD', 'TUSD', 'USDP', 'DAI', 'BUSD', 'USD1', 'RLUSD',\n    'USDE', 'USDS', 'PYUSD', 'AEUR', 'EURI', 'EUR',\n}\n\n"""

if 'STABLE_BASES = {' not in s:
    marker = 'EXCLUDE = {\n'
    if marker not in s:
        raise SystemExit('stablecoin patch failed: EXCLUDE marker missing')
    s = s.replace(marker, stable_block + marker, 1)

old = """def symbol_ok(symbol: str) -> bool:\n    if not symbol.endswith('USDT') or symbol in EXCLUDE:\n        return False\n    base = symbol[:-4]\n    return bool(base) and not base.endswith(('UP', 'DOWN', 'BULL', 'BEAR'))\n"""
new = """def symbol_ok(symbol: str) -> bool:\n    if not symbol.endswith('USDT') or symbol in EXCLUDE:\n        return False\n    base = symbol[:-4]\n    if base in STABLE_BASES:\n        return False\n    return bool(base) and not base.endswith(('UP', 'DOWN', 'BULL', 'BEAR'))\n"""

if old in s:
    s = s.replace(old, new, 1)
elif 'if base in STABLE_BASES:' not in s:
    raise SystemExit('stablecoin patch failed: symbol_ok shape changed')

path.write_text(s, encoding='utf-8')

# Build-time hard assertions: the exact false positive that triggered this patch
# must never reach the momentum engine again, while normal alts remain eligible.
ns = {}
exec(compile(s, str(path), 'exec'), ns)
assert ns['symbol_ok']('UUSDT') is False
assert ns['symbol_ok']('USDCUSDT') is False
assert ns['symbol_ok']('USD1USDT') is False
assert ns['symbol_ok']('RLUSDUSDT') is False
assert ns['symbol_ok']('USDEUSDT') is False
assert ns['symbol_ok']('DOGEUSDT') is True
assert ns['symbol_ok']('SOLUSDT') is True
print('[stablecoin-exclusion] OK U/stablecoins blocked; DOGE/SOL remain eligible')
