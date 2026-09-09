from pathlib import Path

path = Path('/freqtrade/market_context.py')
s = path.read_text(encoding='utf-8')
old = "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
new = "STABLE_BASES = {'U','USDC','FDUSD','TUSD','USDP','USDE','USD1','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
if old in s:
    s = s.replace(old, new, 1)
else:
    # Idempotent upgrade from v2 images which already added USD1.
    old_v2 = "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','USD1','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
    if old_v2 in s:
        s = s.replace(old_v2, new, 1)
    elif not ("'USD1'" in s and "'U'" in s):
        raise SystemExit('market-context-v2: stable base marker missing')
compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[market-context-v2-patch] OK U/USD1 stablecoins excluded from cross-sectional universe')
