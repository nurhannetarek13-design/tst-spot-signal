from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'def fast_momentum_candidates()' in s:
    print('[fast-momentum-discovery] already applied')
    raise SystemExit(0)

const_marker = "MAX_SPREAD_PCT = float(os.getenv('FAST_MAX_SPREAD_PCT', '0.20'))\n"
const_insert = const_marker + (
    "FAST_DISCOVERY_REFRESH_SEC = int(os.getenv('FAST_DISCOVERY_REFRESH_SEC', '60'))\n"
    "FAST_DISCOVERY_MIN_24H_QV = float(os.getenv('FAST_DISCOVERY_MIN_24H_QV', '20000000'))\n"
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

def _fast_spot_usdt_symbols() -> list[str]:
    """Return currently tradable Spot/USDT symbols before ticker requests."""
    info = api('/exchangeInfo', {})
    rows = info.get('symbols') if isinstance(info, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError('exchangeInfo symbols were unavailable')

    symbols: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get('symbol') or '').upper()
        if str(row.get('status') or '').upper() != 'TRADING':
            continue
        if str(row.get('quoteAsset') or '').upper() != 'USDT':
            continue
        if row.get('isSpotTradingAllowed') is False:
            continue
        if not symbol_ok(symbol):
            continue
        symbols.append(symbol)
    return symbols


def _fast_ticker_rows(path: str, symbols: list[str], extra: dict) -> list[dict]:
    """Fetch one all-market ticker snapshot, then filter locally.

    The previous multi-symbol query shape was rejected on the active Binance
    route and caused a RuntimeError/fallback on every refresh. Binance's ticker
    endpoints can return the whole market when `symbol(s)` is omitted; one
    snapshot is faster and internally consistent. If that route ever fails we
    fail over to bounded concurrent single-symbol requests.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    wanted = set(symbols)
    try:
        rows = api(path, dict(extra))
        if isinstance(rows, dict):
            rows = [rows]
        if isinstance(rows, list):
            filtered = [row for row in rows if isinstance(row, dict) and str(row.get('symbol') or '') in wanted]
            if filtered or not symbols:
                return filtered
    except Exception as exc:
        print(f'[fast-discovery] universe-snapshot-fallback path={path} err={type(exc).__name__}')

    def one(symbol: str) -> dict | None:
        params = {'symbol': symbol}
        params.update(extra)
        try:
            row = api(path, params)
            if isinstance(row, dict):
                return row
            if isinstance(row, list) and row and isinstance(row[0], dict):
                return row[0]
        except Exception as exc:
            print(f'[fast-discovery] single-skip path={path} symbol={symbol} err={type(exc).__name__}')
        return None

    result: list[dict] = []
    workers = max(1, min(8, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, symbol) for symbol in symbols]
        for future in as_completed(futures):
            row = future.result()
            if row is not None:
                result.append(row)
    return result


def fast_momentum_candidates() -> list[tuple[str, float, float]]:
    """Discover fresh 5m movers from the liquid Spot/USDT universe.

    Discovery now uses the same high-liquidity philosophy as the original live
    rules: at least 20M USDT 24h quote volume by default. All downstream score,
    ignition, quality, BTC, spread, sizing and confirmation gates remain intact.
    """
    global _fast_discovery_cache_at, _fast_discovery_cache
    now = time.time()
    if _fast_discovery_cache and now - _fast_discovery_cache_at < FAST_DISCOVERY_REFRESH_SEC:
        return list(_fast_discovery_cache)

    try:
        spot_symbols = _fast_spot_usdt_symbols()
        tickers24 = _fast_ticker_rows('/ticker/24hr', spot_symbols, {'type': 'FULL'})
        if not tickers24 and spot_symbols:
            raise RuntimeError('24h ticker requests returned no rows')

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
            if ch24 < -8.0 or ch24 > 25.0:
                continue
            volume24[symbol] = qv
            eligible.append(symbol)

        rolling = _fast_ticker_rows(
            '/ticker',
            eligible,
            {'windowSize': '5m', 'type': 'FULL'},
        )

        found: list[tuple[str, float, float, float]] = []
        for row in rolling:
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

        found.sort(key=lambda x: (x[1], x[3]), reverse=True)
        _fast_discovery_cache = [(sym, pct5, qv24) for sym, pct5, qv24, _ in found[:FAST_DISCOVERY_MAX_CANDIDATES]]
        _fast_discovery_cache_at = now
        preview = ', '.join(f'{sym}:{pct5:+.2f}%' for sym, pct5, _ in _fast_discovery_cache[:8]) or 'none'
        print(f'[fast-discovery] min24h={FAST_DISCOVERY_MIN_24H_QV:.0f} liquid5m={len(_fast_discovery_cache)} top={preview}')
        return list(_fast_discovery_cache)
    except Exception as exc:
        print(f'[fast-discovery] warning {type(exc).__name__}: {exc}')
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
    # Fast 5m discovery comes first so fresh liquid momentum enters the scoring
    # queue early. Normal scanner candidates then fill the remaining slots.
    # All downstream quality/ignition gates are unchanged.
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
    'def _fast_spot_usdt_symbols()',
    'def _fast_ticker_rows(',
    'def fast_momentum_candidates()',
    "FAST_DISCOVERY_MIN_24H_QV = float(os.getenv('FAST_DISCOVERY_MIN_24H_QV', '20000000'))",
    "FAST_DISCOVERY_REFRESH_SEC = int(os.getenv('FAST_DISCOVERY_REFRESH_SEC', '60'))",
    "qv < FAST_DISCOVERY_MIN_24H_QV",
    "'windowSize': '5m'",
    "api('/exchangeInfo', {})",
    'ThreadPoolExecutor',
    'universe-snapshot-fallback',
    '[fast-discovery]',
    'for symbol, pct5, volume in fast_momentum_candidates()',
    "FAST_DISCOVERY_MIN_5M_PCT",
]:
    if required not in s:
        raise SystemExit(f'fast momentum discovery patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[fast-momentum-discovery] OK all-market ticker snapshots + 20M 24h liquidity floor + bounded single-symbol failover')
