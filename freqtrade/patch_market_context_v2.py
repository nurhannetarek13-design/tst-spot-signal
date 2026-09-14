from pathlib import Path

path = Path('/freqtrade/market_context.py')
s = path.read_text(encoding='utf-8')
old = "STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','DAI','BUSD','EUR','AEUR','TRY','BRL'}"
non_crypto = {
    'U','USDC','FDUSD','TUSD','USDP','USDE','USD1','RLUSD','DAI','BUSD','EUR','AEUR','TRY','BRL','PAXG','XAUT',
    'AAOIB','AAPLB','ALABB','AMATB','AMDB','AMZNB','ARMB','ASMLB','ASTSB','AVGOB','AXTIB',
    'BABAB','BEB','BMNRB','CBRSB','COHRB','COINB','CRCLB','CRDOB','CRWDB','CRWVB',
    'DELLB','DJTB','DRAMB','EWYB','FLNCB','GLWB','GMEB','GOOGLB','GSB','HIMSB','HOODB',
    'IBMB','INTCB','INTWB','IRENB','KORUB','LITEB','METAB','MRNAB','MRVLB','MSFTB','MSTRB',
    'MUB','MUUB','MVLLB','NBISB','NFLXB','NOKB','NVDAB','ORCLB','PLTRB','PYPLB','QCOMB',
    'QNTB','QQQB','RKLBB','SKHYB','SMCIB','SMHB','SNDKB','SNXXB','SOXLB','SOXSB','SPCXB',
    'SPYB','SQQQB','STXB','TQQQB','TSLAB','TSMB','USARB','WDCB','CRMB',
}
new = 'STABLE_BASES = {' + ','.join(repr(x) for x in sorted(non_crypto)) + '}'
if old in s:
    s = s.replace(old, new, 1)
else:
    # Upgrade any earlier patched image by replacing the whole constant line.
    lines = s.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith('STABLE_BASES = {'):
            lines[i] = new
            replaced = True
            break
    if not replaced:
        raise SystemExit('market-context-v2: non-crypto base marker missing')
    s = '\n'.join(lines) + ('\n' if s.endswith('\n') else '')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print(f'[market-context-v2-patch] OK stable/fiat/metals + {74} Binance bStocks excluded from cross-sectional crypto ranking')
