from __future__ import annotations

"""Live market-context collector for research-grade regime/relative-strength telemetry.

IMPORTANT: this module is SHADOW/RESEARCH CONTEXT ONLY. It does not place orders
and does not block live entries. It continuously builds a point-in-time snapshot
from Binance Spot public data so accepted/rejected candidates can later be tested
against regime, breadth and cross-sectional relative-strength features.
"""

import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SNAPSHOT_PATH = Path(os.getenv('TST_MARKET_CONTEXT_PATH', '/data/tst_market_context.json'))
POLL_SEC = max(60, int(os.getenv('MARKET_CONTEXT_POLL_SEC', '180')))
TOP_N = max(30, min(140, int(os.getenv('MARKET_CONTEXT_TOP_N', '80'))))
MIN_QV = max(1_000_000.0, float(os.getenv('MARKET_CONTEXT_MIN_QV', '10000000')))
MAX_WORKERS = max(4, min(16, int(os.getenv('MARKET_CONTEXT_WORKERS', '10'))))
PUBLIC_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']
STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','USDE','DAI','BUSD','EUR','AEUR','TRY','BRL'}


def _get(path: str, params: dict | None = None):
    query = urllib.parse.urlencode(params or {})
    last = None
    for base in PUBLIC_BASES:
        try:
            url = f'{base}{path}' + (f'?{query}' if query else '')
            req = urllib.request.Request(url, headers={'User-Agent':'tst-market-context/1.0','Accept':'application/json'})
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read() or b'{}')
        except Exception as exc:
            last = exc
    raise RuntimeError(f'BINANCE_PUBLIC_UNAVAILABLE:{type(last).__name__ if last else "unknown"}')


def _atomic_write(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(row, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def _eligible_tickers() -> list[tuple[str, float]]:
    rows = _get('/ticker/24hr')
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            symbol = str(row.get('symbol') or '').upper()
            if not symbol.endswith('USDT') or len(symbol) <= 4:
                continue
            base = symbol[:-4]
            if not base.isalnum() or base in STABLE_BASES:
                continue
            qv = float(row.get('quoteVolume') or 0)
            if qv < MIN_QV:
                continue
            out.append((symbol, qv))
        except Exception:
            continue
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:TOP_N]


def _features(symbol: str, qv: float) -> dict | None:
    rows = _get('/klines', {'symbol':symbol, 'interval':'1h', 'limit':26})
    if not isinstance(rows, list) or len(rows) < 25:
        return None
    try:
        h = [float(x[2]) for x in rows]
        l = [float(x[3]) for x in rows]
        c = [float(x[4]) for x in rows]
        q = [float(x[7]) for x in rows]
    except Exception:
        return None
    if min(c[-25:]) <= 0:
        return None

    def ret(n: int) -> float:
        return c[-1] / c[-1-n] - 1.0

    r1 = ret(1)
    prev1 = c[-2] / c[-3] - 1.0
    r4 = ret(4)
    r24 = ret(24)
    accel = r1 - prev1
    ranges = [(h[i]-l[i]) / c[i-1] if i > 0 and c[i-1] > 0 else 0.0 for i in range(1, len(c))]
    atr6 = statistics.mean(ranges[-6:]) if len(ranges) >= 6 else 0.0
    atr24 = statistics.mean(ranges[-24:]) if len(ranges) >= 24 else (statistics.mean(ranges) if ranges else 0.0)
    vol_ratio = atr6 / atr24 if atr24 > 1e-12 else 1.0
    q_recent = statistics.mean(q[-4:]) if len(q) >= 4 else q[-1]
    q_prior = statistics.mean(q[-12:-4]) if len(q) >= 12 else q_recent
    volume_expansion = q_recent / q_prior if q_prior > 1e-12 else 1.0
    return {
        'symbol': symbol,
        'quote_volume_24h': qv,
        'r1h': r1,
        'r4h': r4,
        'r24h': r24,
        'accel1h': accel,
        'atr6h_pct': atr6,
        'atr24h_pct': atr24,
        'volatility_ratio': vol_ratio,
        'volume_expansion_4h_vs_8h': volume_expansion,
        'close': c[-1],
    }


def _pct_rank(values: list[tuple[str, float]]) -> dict[str, float]:
    valid = [(s, v) for s, v in values if math.isfinite(v)]
    if not valid:
        return {}
    valid.sort(key=lambda x: x[1])
    n = len(valid)
    if n == 1:
        return {valid[0][0]: 50.0}
    return {s: 100.0 * i / (n-1) for i, (s, _) in enumerate(valid)}


def _regime(btc: dict, breadth1: float, breadth4: float, median4: float) -> str:
    """Heuristic label for collection only; never treated as validated edge."""
    r1 = float(btc.get('r1h') or 0)
    r4 = float(btc.get('r4h') or 0)
    r24 = float(btc.get('r24h') or 0)
    vr = float(btc.get('volatility_ratio') or 1)
    if (r1 <= -0.015 or r4 <= -0.03) and vr >= 1.25 and breadth1 <= 0.30:
        return 'PANIC_HIGH_VOL_BEAR'
    if r24 <= -0.035 and r1 >= 0.004 and breadth1 >= 0.55 and median4 > -0.005:
        return 'POST_CRASH_RECOVERY'
    if r4 >= 0.012 and r24 >= 0.02 and breadth4 >= 0.62:
        return 'STRONG_BULL'
    if r4 > 0 and breadth4 >= 0.50:
        return 'WEAK_BULL'
    if r4 <= -0.006 and breadth4 <= 0.42:
        return 'WEAK_BEAR'
    return 'SIDEWAYS_COMPRESSION'


def build_snapshot() -> dict:
    tickers = _eligible_tickers()
    feats: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_features, symbol, qv): symbol for symbol, qv in tickers}
        for fut in as_completed(futs):
            try:
                row = fut.result()
                if row:
                    feats.append(row)
            except Exception:
                pass
    by_symbol = {r['symbol']: r for r in feats}
    btc = by_symbol.get('BTCUSDT')
    if not btc:
        btc = _features('BTCUSDT', 0.0)
        if btc:
            by_symbol['BTCUSDT'] = btc
            feats.append(btc)
    if not btc or len(feats) < 20:
        raise RuntimeError(f'INSUFFICIENT_CONTEXT_SAMPLE:{len(feats)}')

    btc4 = float(btc['r4h'])
    for r in feats:
        r['rs_vs_btc_4h'] = float(r['r4h']) - btc4

    ranks = {
        'r1h_pct': _pct_rank([(r['symbol'], float(r['r1h'])) for r in feats]),
        'r4h_pct': _pct_rank([(r['symbol'], float(r['r4h'])) for r in feats]),
        'rs_btc4h_pct': _pct_rank([(r['symbol'], float(r['rs_vs_btc_4h'])) for r in feats]),
        'accel1h_pct': _pct_rank([(r['symbol'], float(r['accel1h'])) for r in feats]),
        'volume_expansion_pct': _pct_rank([(r['symbol'], float(r['volume_expansion_4h_vs_8h'])) for r in feats]),
        'liquidity_pct': _pct_rank([(r['symbol'], math.log1p(float(r['quote_volume_24h']))) for r in feats]),
    }
    for r in feats:
        s = r['symbol']
        for name, table in ranks.items():
            r[name] = round(float(table.get(s, 0.0)), 3)
        # Equal-weight discovery composite only. It is NOT a probability and is
        # deliberately not connected to live position sizing or entry blocking.
        r['opportunity_pct_shadow'] = round(statistics.mean([
            r['r1h_pct'], r['r4h_pct'], r['rs_btc4h_pct'], r['accel1h_pct'], r['volume_expansion_pct']
        ]), 3)

    breadth1 = sum(1 for r in feats if float(r['r1h']) > 0) / len(feats)
    breadth4 = sum(1 for r in feats if float(r['r4h']) > 0) / len(feats)
    median1 = statistics.median(float(r['r1h']) for r in feats)
    median4 = statistics.median(float(r['r4h']) for r in feats)
    regime = _regime(btc, breadth1, breadth4, median4)
    now = time.time()
    symbols = {}
    for r in feats:
        symbols[r['symbol']] = {
            k: (round(v, 8) if isinstance(v, float) else v)
            for k, v in r.items() if k != 'symbol'
        }
    return {
        'version': 1,
        'generated_at': now,
        'method': 'HEURISTIC_SHADOW_V1_NOT_LIVE_GATE',
        'universe_n': len(feats),
        'regime': regime,
        'trade_permission_shadow': regime != 'PANIC_HIGH_VOL_BEAR',
        'breadth_1h': round(breadth1, 6),
        'breadth_4h': round(breadth4, 6),
        'median_return_1h': round(median1, 8),
        'median_return_4h': round(median4, 8),
        'btc': symbols.get('BTCUSDT', {}),
        'symbols': symbols,
    }


def load_snapshot(max_age_sec: int = 900) -> dict:
    try:
        row = json.loads(SNAPSHOT_PATH.read_text(encoding='utf-8'))
        if not isinstance(row, dict):
            return {}
        if time.time() - float(row.get('generated_at') or 0) > max_age_sec:
            return {}
        return row
    except Exception:
        return {}


def symbol_context(symbol: str) -> dict:
    snap = load_snapshot()
    if not snap:
        return {}
    r = ((snap.get('symbols') or {}).get(str(symbol or '').upper()) or {})
    if not isinstance(r, dict):
        r = {}
    return {
        'regime': snap.get('regime'),
        'trade_permission_shadow': snap.get('trade_permission_shadow'),
        'breadth_1h': snap.get('breadth_1h'),
        'breadth_4h': snap.get('breadth_4h'),
        'universe_n': snap.get('universe_n'),
        'rs_vs_btc_4h': r.get('rs_vs_btc_4h'),
        'r1h_pct_rank': r.get('r1h_pct'),
        'r4h_pct_rank': r.get('r4h_pct'),
        'rs_btc4h_pct_rank': r.get('rs_btc4h_pct'),
        'accel1h_pct_rank': r.get('accel1h_pct'),
        'volume_expansion_pct_rank': r.get('volume_expansion_pct'),
        'liquidity_pct_rank': r.get('liquidity_pct'),
        'opportunity_pct_shadow': r.get('opportunity_pct_shadow'),
        'context_generated_at': snap.get('generated_at'),
    }


def run_once() -> dict:
    snap = build_snapshot()
    _atomic_write(SNAPSHOT_PATH, snap)
    top = sorted(
        ((s, float((r or {}).get('opportunity_pct_shadow') or 0)) for s, r in (snap.get('symbols') or {}).items()),
        key=lambda x: x[1], reverse=True,
    )[:5]
    print(
        f"[market-context] regime={snap['regime']} n={snap['universe_n']} "
        f"breadth1h={snap['breadth_1h']:.2f} breadth4h={snap['breadth_4h']:.2f} "
        f"top=" + ','.join(f'{s}:{p:.0f}' for s,p in top),
        flush=True,
    )
    return snap


def main() -> None:
    print(f'[market-context] ONLINE shadow_only=True poll={POLL_SEC}s top_n={TOP_N} min_qv={MIN_QV:.0f}', flush=True)
    while True:
        started = time.time()
        try:
            run_once()
        except Exception as exc:
            print(f'[market-context] warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(max(5.0, POLL_SEC - (time.time()-started)))


if __name__ == '__main__':
    if '--once' in sys.argv:
        run_once()
    else:
        main()
