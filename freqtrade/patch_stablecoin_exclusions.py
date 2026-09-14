from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Keep this guard deliberately explicit. The strategy is crypto Spot only:
# stable/fiat-like assets and Binance bStocks/tokenized securities must never
# reach any momentum/reversal lane, regardless of score or liquidity.
stable_extra = [
    'UUSDT',
    'USDEUSDT',
    'USDSUSDT',
    'PYUSDUSDT',
    'EURIUSDT',
]

# Official Binance bStocks symbols observed through the current Sep-2026
# listings. Explicit symbols are safer than a broad "endswith B" heuristic,
# which would incorrectly reject normal crypto such as ARB.
bstock_bases = [
    'AAOIB','AAPLB','ALABB','AMATB','AMDB','AMZNB','ARMB','ASMLB','ASTSB','AVGOB','AXTIB',
    'BABAB','BEB','BMNRB','CBRSB','COHRB','COINB','CRCLB','CRDOB','CRWDB','CRWVB',
    'DELLB','DJTB','DRAMB','EWYB','FLNCB','GLWB','GMEB','GOOGLB','GSB','HIMSB','HOODB',
    'IBMB','INTCB','INTWB','IRENB','KORUB','LITEB','METAB','MRNAB','MRVLB','MSFTB','MSTRB',
    'MUB','MUUB','MVLLB','NBISB','NFLXB','NOKB','NVDAB','ORCLB','PLTRB','PYPLB','QCOMB',
    'QNTB','QQQB','RKLBB','SKHYB','SMCIB','SMHB','SNDKB','SNXXB','SOXLB','SOXSB','SPCXB',
    'SPYB','SQQQB','STXB','TQQQB','TSLAB','TSMB','USARB','WDCB','CRMB',
]
bstock_extra = [base + 'USDT' for base in bstock_bases]
extra = stable_extra + bstock_extra

marker = 'EXCLUDE = {\n'
if marker not in s:
    raise SystemExit('universe exclusion patch failed: EXCLUDE marker missing')

missing = [sym for sym in extra if f"'{sym}'" not in s]
if missing:
    injected = ''.join(f"    {sym!r},\n" for sym in missing)
    s = s.replace(marker, marker + injected, 1)

path.write_text(s, encoding='utf-8')

# Build-time hard assertions. CRCLB is the exact false-positive that caused a
# real losing trade and must never be eligible again. Normal crypto stays open.
ns = {}
exec(compile(s, str(path), 'exec'), ns)
for sym in [
    'UUSDT','USDCUSDT','FDUSDUSDT','USD1USDT','RLUSDUSDT','USDEUSDT','USDSUSDT','PYUSDUSDT','EURIUSDT',
    'CRCLBUSDT','NVDABUSDT','TSLABUSDT','MSTRBUSDT','QQQBUSDT','SPYBUSDT','CRWDBUSDT','HIMSBUSDT','CRMBUSDT',
]:
    assert ns['symbol_ok'](sym) is False, sym
for sym in ['DOGEUSDT','SOLUSDT','ARBUSDT','BNBUSDT','ZECUSDT']:
    assert ns['symbol_ok'](sym) is True, sym
print(f'[universe-exclusion] OK stablecoins + {len(bstock_extra)} Binance bStocks/tokenized securities blocked; crypto Spot remains eligible')
