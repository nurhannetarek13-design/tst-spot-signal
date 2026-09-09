from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'def fast_momentum_candidates()' in s:
    print('[fast-momentum-discovery] already applied')
    raise SystemExit(0)

const_marker = "MAX_SPREAD_PCT = float(os.getenv('FAST_MAX_SPREAD_PCT', '0.20'))\n"
const_insert = const_marker + (
    "FAST_DISCOVERY_REFRESH_SEC = int(os.getenv('FAST_DISCOVERY_REFRESH_SEC', '45'))\n"
    "FAST_DISCOVERY_MIN_24H_QV = float(os.getenv('FAST_DISCOVERY_MIN_24H_QV', '3000000'))\n"
    "FAST_DISCOVERY_MIN_5M_PCT = float(os.getenv('FAST_DISCOVERY_MIN_5M_PCT', '0.08'))\n"
    "FAST_DISCOVERY_MAX_5M_PCT = float(os.getenv('FAST_DISCOVERY_MAX_5M_PCT', '1.50'))\n"
    "FAST_DISCOVERY_MIN_5M_QV = float(os.getenv('FAST_DISCOVERY_MIN_5M_QV', '10000'))\n"
    "FAST_DISCOVERY_MAX_CANDIDATES = int(os.getenv('FAST_DISCOVERY_MAX_CANDIDATES', '35'))\n"
)
if const_marker not in s:
    raise SystemExit('fast momentum discovery patch failed: constants marker missing')
s = s.replace(const_marker, const_insert, 1)

state_marker = "execution_ready = False\n"
state_insert = state_marker + "_fast_discovery_cache_at = 0.0\n_fast_discovery_cache: list[tuple[str, float, float]] = []\n"
if state_marker not in s:
    raise SystemExit('fast momentum discovery patch failed: state marker missing')
s = s.replace(state_marker, state_insert, 1)

candidate_marker = "\ndef candidate_symbols(scan: dict) -> list[tuple[str, float, float]]:\n"
helper = r'''

def fast_momentum_candidates() -> list[tuple[str, float, float]]:
    """Discover fresh 5m movers from the full liquid USDT universe.

    The normal scanner keeps its stricter 24h liquidity floor. This discovery
    layer deliberately uses a separate 3M USDT/24h floor so mid-caps such as
    COT can enter the scoring queue early, while all score, 5m turnover,
    ignition, quality, BTC, spread, sizing and user-confirmation gates remain.
    """
    global _fast_discovery_cache_at, _fast_discovery_cache
    now = time.time()
    if _fast_discovery_cache and now - _fast_discovery_cache_at < FAST_DISCOVERY_REFRESH_SEC:
        return list(_fast_discovery_cache)

    try:
        tickers24 = api('/ticker/24hr', {})
        if not isinstance(tickers24, list):
            raise RuntimeError('24h ticker universe was not a list')

        volume24: dict[str, float] = {}
        eligible: list[str] = []
        for row in tickers24:
            symbol = str(row.get('symbol') or '')
            if not symbol_ok(symbol):
                continue
            qv = float(row.get('quoteVolume') or 0.0)
            ch24 = float(row.get('priceChangePercent') or 0.0)
            if qv < FAST_DISCOVERY_MIN_24H_QV:
                continue
            # Discovery should be broad, but avoid assets already in a huge
            # 24h blow-off move. The final anti-chase gate remains stricter.
            if ch24 < -8.0 or ch24 > 25.0:
                continue
            volume24[symbol] = qv
            eligible.append(symbol)

        found: list[tuple[str, float, float, float]] = []
        for i in range(0, len(eligible), 100):
            batch = eligible[i:i + 100]
            if not batch:
                continue
            rows = api('/ticker', {
                'symbols': json.dumps(batch, separators=(',', ':')),
                'windowSize': '5m',
                'type': 'FULL',
            })
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows or []:
                symbol = str(row.get('symbol') or '')
                if symbol not in volume24 or not symbol_ok(symbol):
                    continue
                pct5 = float(row.get('priceChangePercent') or 0.0)
                qv5 = float(row.get('quoteVolume') or 0.0)
                if pct5 < FAST_DISCOVERY_MIN_5M_PCT or pct5 > FAST_DISCOVERY_MAX_5M_PCT:
                    continue
                if qv5 < FAST_DISCOVERY_MIN_5M_QV:
                    continue
                found.append((symbol, pct5, volume24[symbol], qv5))

        # Fresh % move first, then real 5m turnover. This is only the queue for
        # expensive microstructure scoring, not the trading score itself.
        found.sort(key=lambda x: (x[1], x[3]), reverse=True)
        _fast_discovery_cache = [(sym, pct5, qv24) for sym, pct5, qv24, _ in found[:FAST_DISCOVERY_MAX_CANDIDATES]]
        _fast_discovery_cache_at = now
        preview = ', '.join(f'{sym}:{pct5:+.2f}%' for sym, pct5, _ in _fast_discovery_cache[:8]) or 'none'
        print(f'[fast-discovery] min24h={FAST_DISCOVERY_MIN_24H_QV:.0f} liquid5m={len(_fast_discovery_cache)} top={preview}')
        return list(_fast_discovery_cache)
    except Exception as exc:
        print(f'[fast-discovery] warning {type(exc).__name__}: {exc}')
        # Do not break the normal scanner if rolling-window discovery has a
        # transient API failure. A recent cache is safer than fail-open trades.
        return list(_fast_discovery_cache)

'''
if candidate_marker not in s:
    raise SystemExit('fast momentum discovery patch failed: candidate marker missing')
s = s.replace(candidate_marker, helper + candidate_marker, 1)

old_start = '''def candidate_symbols(scan: dict) -> list[tuple[str, float, float]]:
    # Liquidity first. 24h winners are not automatically better; large positive change
    # is often exactly what makes us late.
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()

    mover_map = {}
'''
new_start = '''def candidate_symbols(scan: dict) -> list[tuple[str, float, float]]:
    # Fast 5m discovery comes first so a liquid mid-cap can enter the scoring
    # queue before its 24h rank catches up. Normal scanner candidates then fill
    # the remaining slots. All downstream quality/ignition gates are unchanged.
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()

    for symbol, pct5, volume in fast_momentum_candidates():
        if not symbol_ok(symbol) or symbol in seen:
            continue
        out.append((symbol, pct5, volume))
        seen.add(symbol)
        if len(out) >= 50:
            return out[:50]

    mover_map = {}
'''
if old_start not in s:
    raise SystemExit('fast momentum discovery patch failed: candidate function body marker missing')
s = s.replace(old_start, new_start, 1)

for required in [
    'def fast_momentum_candidates()',
    "FAST_DISCOVERY_MIN_24H_QV = float(os.getenv('FAST_DISCOVERY_MIN_24H_QV', '3000000'))",
    'qv < FAST_DISCOVERY_MIN_24H_QV',
    "'windowSize': '5m'",
    '[fast-discovery]',
    'for symbol, pct5, volume in fast_momentum_candidates()',
    "FAST_DISCOVERY_MIN_5M_PCT",
]:
    if required not in s:
        raise SystemExit(f'fast momentum discovery patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[fast-momentum-discovery] OK 3M 24h discovery floor + 5m turnover -> existing ignition/quality gates')
