from pathlib import Path

path = Path('/freqtrade/market_context.py')
s = path.read_text(encoding='utf-8')
old = "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
new = "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','USD1','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
if old in s:
    s = s.replace(old, new, 1)
elif "'USD1'" not in s:
    raise SystemExit('market-context-v2: stable base marker missing')
compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[market-context-v2-patch] OK USD1 excluded from cross-sectional universe')
