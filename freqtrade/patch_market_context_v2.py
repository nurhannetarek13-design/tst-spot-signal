from pathlib import Path

path = Path('/freqtrade/market_context.py')
s = path.read_text(encoding='utf-8')
old = "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
new = "STABLE_BASES = {'U','USDC','FDUSD','TUSD','USDP','USDE','USD1','RLUSD','DAI','BUSD','EUR','AEUR','TRY','BRL','PAXG','XAUT'}"
if old in s:
    s = s.replace(old, new, 1)
else:
    # Idempotent upgrade from earlier patched images.
    candidates = [
        "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','USD1','DAI','BUSD','EUR','AEUR','TRY','BRL'}",
        "STABLE_BASES = {'U','USDC','FDUSD','TUSD','USDP','USDE','USD1','DAI','BUSD','EUR','AEUR','TRY','BRL'}",
    ]
    replaced = False
    for marker in candidates:
        if marker in s:
            s = s.replace(marker, new, 1)
            replaced = True
            break
    if not replaced and not all(x in s for x in ("'USD1'", "'U'", "'RLUSD'", "'PAXG'", "'XAUT'")):
        raise SystemExit('market-context-v2: stable/non-crypto base marker missing')
compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[market-context-v2-patch] OK U/USD1/RLUSD stablecoins + PAXG/XAUT non-crypto assets excluded from cross-sectional universe')
