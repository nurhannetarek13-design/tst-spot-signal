#!/usr/bin/env python3
"""Research-only intraday barrier edge scanner.

Goal: test whether current fast-bot style setups have a repeatable statistical
edge before changing any live rule. Uses only information known at bar close and
enters on the next 15m open. Same-bar TP+SL touches are resolved pessimistically
as SL first.

Validation design:
- broad liquid Spot USDT universe
- point-in-time 24h liquidity eligibility per event
- deterministic symbol holdout (20%) never used for discovery
- chronological 60/20/20 discovery/calibration/test split
- predeclared setup families and two fixed TP/SL contracts
- Benjamini-Hochberg FDR on discovery hypotheses
- no automatic live promotion under any result
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

BASE_URL = 'https://data-api.binance.vision'
INTERVAL = '15m'
BAR_MS = 15 * 60 * 1000
DAYS = 120
MAX_SYMBOLS = 50
CURRENT_MIN_QV = 8_000_000
EVENT_MIN_QV24 = 3_000_000
DOWNLOAD_WORKERS = 8
API_RETRIES = 4
FDR_Q = 0.10
MIN_DISCOVERY_EVENTS = 80
MIN_VALIDATION_EVENTS = 30
OUT = pathlib.Path('validation/edges/intraday-barrier-edge-v1-latest.json')
EXCLUDED = {'USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE','PAXG','XAUT'}

# Two fixed contracts reflect the live system's intended intraday reward scale.
CONTRACTS = {
    'NORMAL_15_10': {'tp': 0.015, 'sl': 0.010, 'horizon_bars': 16},   # 4h
    'EXPANSION_22_14': {'tp': 0.022, 'sl': 0.014, 'horizon_bars': 32}, # 8h
}
ROUND_TRIP_COST = 0.0028


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(path):
    last = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            req = urllib.request.Request(BASE_URL + path, headers={'User-Agent': 'tst-intraday-barrier-edge-v1/1.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            if attempt < API_RETRIES:
                time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
    raise RuntimeError(f'API failed after {API_RETRIES}: {path}: {last}')


def universe():
    info = api('/api/v3/exchangeInfo')
    tick = {x['symbol']: x for x in api('/api/v3/ticker/24hr')}
    rows = []
    for s in info.get('symbols', []):
        base = str(s.get('baseAsset') or '')
        if s.get('status') != 'TRADING' or s.get('quoteAsset') != 'USDT' or not s.get('isSpotTradingAllowed'):
            continue
        if not base or base in EXCLUDED or base.endswith(('UP','DOWN','BULL','BEAR')):
            continue
        qv = float(tick.get(s['symbol'], {}).get('quoteVolume') or 0)
        if qv >= CURRENT_MIN_QV:
            rows.append((s['symbol'], qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:MAX_SYMBOLS]]


def klines(symbol):
    end = int(time.time() * 1000)
    cur = end - DAYS * 86400000
    rows = []
    while cur < end:
        q = urllib.parse.urlencode({'symbol': symbol, 'interval': INTERVAL, 'limit': 1000, 'startTime': cur, 'endTime': end})
        batch = api('/api/v3/klines?' + q)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + BAR_MS
        if nxt <= cur:
            break
        cur = nxt
    if len(rows) < 2500:
        raise RuntimeError(f'{symbol}: insufficient bars {len(rows)}')
    d = pd.DataFrame(rows, columns=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'])
    for c in ['open','high','low','close','volume','quote_volume','taker_quote']:
        d[c] = pd.to_numeric(d[c], errors='coerce')
    d['ts'] = pd.to_datetime(d.open_time, unit='ms', utc=True)
    d['symbol'] = symbol
    return d[['ts','symbol','open','high','low','close','volume','quote_volume','taker_quote']].dropna()


def rsi(series, n=14):
    diff = series.diff()
    up = diff.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def add_symbol_features(d, btc):
    d = d.sort_values('ts').copy()
    c = d.close
    d['ret_1h'] = c.pct_change(4)
    d['ret_4h'] = c.pct_change(16)
    d['ret_12h'] = c.pct_change(48)
    d['accel_1h'] = d.ret_1h - d.ret_1h.shift(4)
    d['qv24'] = d.quote_volume.rolling(96, min_periods=48).sum()
    d['volume_ratio'] = d.quote_volume / d.quote_volume.rolling(32, min_periods=16).median().replace(0, np.nan)
    d['taker_buy_ratio'] = d.taker_quote / d.quote_volume.replace(0, np.nan)
    d['vol_4h'] = c.pct_change().rolling(16, min_periods=12).std(ddof=0)
    d['rsi14'] = rsi(c, 14)
    d['ema9'] = c.ewm(span=9, adjust=False).mean()
    d['ema21'] = c.ewm(span=21, adjust=False).mean()
    prior24_high = d.high.rolling(96, min_periods=48).max().shift(1)
    d['breakout_24h'] = c / prior24_high - 1
    d['pullback_4h'] = c / d.high.rolling(16, min_periods=8).max() - 1
    b = btc[['ts','close']].rename(columns={'close': 'btc_close'})
    d = d.merge(b, on='ts', how='left')
    d['btc_close'] = d.btc_close.ffill()
    d['btc_ret_1h'] = d.btc_close.pct_change(4)
    d['btc_ret_4h'] = d.btc_close.pct_change(16)
    d['rs_1h'] = d.ret_1h - d.btc_ret_1h
    d['rs_4h'] = d.ret_4h - d.btc_ret_4h
    return d


def add_cross_sectional(frame):
    d = frame.copy()
    for c in ['ret_1h','ret_4h','rs_1h','rs_4h','accel_1h','volume_ratio','vol_4h','qv24']:
        d[c + '_rank'] = d.groupby('ts')[c].rank(pct=True, method='average')
    return d


def setups(d):
    # Predeclared, intentionally compact families. They are evaluated as-is.
    return {
        'TREND_RS': (
            (d.ret_4h_rank >= .80) & (d.rs_4h_rank >= .80) &
            (d.volume_ratio >= 1.10) & (d.taker_buy_ratio >= .54) & (d.close >= d.ema21)
        ),
        'FIRST_BREAK': (
            (d.breakout_24h >= 0.0) & (d.breakout_24h <= .012) &
            (d.ret_1h > 0) & (d.volume_ratio >= 1.15) &
            (d.taker_buy_ratio >= .56) & (d.rsi14 <= 74)
        ),
        'PULLBACK_RECLAIM': (
            (d.ret_4h >= .025) & (d.pullback_4h <= -.008) & (d.pullback_4h >= -.05) &
            (d.close >= d.ema9) & (d.ema9 >= d.ema21 * .997) &
            (d.taker_buy_ratio >= .55) & (d.rsi14 <= 72)
        ),
        'VOL_EXPANSION': (
            (d.vol_4h_rank >= .80) & (d.ret_1h_rank >= .80) &
            (d.volume_ratio_rank >= .75) & (d.taker_buy_ratio >= .56) & (d.rsi14 <= 76)
        ),
        'RS_ACCELERATION': (
            (d.rs_1h_rank >= .85) & (d.accel_1h_rank >= .80) &
            (d.volume_ratio >= 1.05) & (d.taker_buy_ratio >= .55) & (d.close >= d.ema9)
        ),
        'DEEP_REVERSAL': (
            (d.ret_4h_rank <= .10) & (d.rsi14 <= 32) &
            (d.taker_buy_ratio >= .55) & (d.accel_1h > 0) & (d.volume_ratio >= 1.05)
        ),
    }


def symbol_holdout(symbol):
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16) % 5 == 0


def barrier_result(g, idx, contract):
    if idx + 1 >= len(g):
        return None
    entry = float(g.open.iloc[idx + 1])
    if not math.isfinite(entry) or entry <= 0:
        return None
    tp = entry * (1 + contract['tp'])
    sl = entry * (1 - contract['sl'])
    end = min(len(g) - 1, idx + contract['horizon_bars'])
    if end <= idx:
        return None
    outcome = 'TIMEOUT'
    gross = None
    held = 0
    mfe = -1e9
    mae = 0.0
    for j in range(idx + 1, end + 1):
        hi = float(g.high.iloc[j]); lo = float(g.low.iloc[j]); held += 1
        mfe = max(mfe, hi / entry - 1)
        mae = max(mae, 1 - lo / entry)
        hit_sl = lo <= sl
        hit_tp = hi >= tp
        if hit_sl and hit_tp:
            outcome = 'SL_AMBIGUOUS'; gross = -contract['sl']; break
        if hit_sl:
            outcome = 'SL'; gross = -contract['sl']; break
        if hit_tp:
            outcome = 'TP'; gross = contract['tp']; break
    if gross is None:
        last = float(g.close.iloc[end]) / entry - 1
        gross = max(-contract['sl'], min(contract['tp'], last))
    return {
        'gross': gross, 'net': gross - ROUND_TRIP_COST, 'outcome': outcome,
        'held_bars': held, 'mfe': max(0.0, mfe), 'mae': max(0.0, mae),
    }


def one_sided_mean_p(xs):
    if len(xs) < 2:
        return 1.0
    a = np.asarray(xs, float)
    sd = float(a.std(ddof=1))
    if sd <= 0:
        return 0.0 if float(a.mean()) > 0 else 1.0
    z = float(a.mean()) / (sd / math.sqrt(len(a)))
    return 0.5 * math.erfc(z / math.sqrt(2))


def summarize(rows):
    if not rows:
        return {'n': 0}
    net = [r['net'] for r in rows]
    pos = sum(max(0.0, x) for x in net); neg = sum(max(0.0, -x) for x in net)
    return {
        'n': len(rows),
        'meanNet': float(np.mean(net)),
        'medianNet': float(np.median(net)),
        'profitFactor': pos / neg if neg > 0 else (999.0 if pos > 0 else 0.0),
        'tpRate': sum(r['outcome'] == 'TP' for r in rows) / len(rows),
        'slRate': sum(r['outcome'] in {'SL','SL_AMBIGUOUS'} for r in rows) / len(rows),
        'timeoutRate': sum(r['outcome'] == 'TIMEOUT' for r in rows) / len(rows),
        'medianMFE': float(np.median([r['mfe'] for r in rows])),
        'medianMAE': float(np.median([r['mae'] for r in rows])),
        'pMeanPositive': one_sided_mean_p(net),
    }


def bh(rows):
    order = sorted(range(len(rows)), key=lambda i: rows[i]['discovery']['pMeanPositive'])
    m = len(order); adj = [1.0] * len(rows); running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        p = rows[i]['discovery']['pMeanPositive']
        running = min(running, p * m / rank)
        adj[i] = min(1.0, running)
    for i, r in enumerate(rows):
        r['qValue'] = adj[i]
        d = r['discovery']
        r['discoveryPass'] = bool(
            d['n'] >= MIN_DISCOVERY_EVENTS and d['meanNet'] > 0 and
            d['profitFactor'] >= 1.15 and adj[i] <= FDR_Q
        )


def validation_pass(s):
    return bool(s.get('n', 0) >= MIN_VALIDATION_EVENTS and s.get('meanNet', -1) > 0 and s.get('profitFactor', 0) >= 1.05)


def main():
    started = time.time()
    syms = universe()
    if 'BTCUSDT' not in syms:
        all_syms = ['BTCUSDT'] + syms
    else:
        all_syms = syms[:]
    log(f'universe={len(syms)} downloading={len(all_syms)}')
    data = {}; failures = {}
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        fut = {ex.submit(klines, s): s for s in all_syms}
        done = 0
        for f in as_completed(fut):
            s = fut[f]; done += 1
            try:
                data[s] = f.result(); log(f'data {done}/{len(all_syms)} {s} bars={len(data[s])}')
            except Exception as exc:
                failures[s] = str(exc); log(f'data {done}/{len(all_syms)} {s} FAIL {exc}')
    if 'BTCUSDT' not in data:
        raise RuntimeError('BTC history unavailable')
    loaded = [s for s in syms if s in data and s != 'BTCUSDT']
    frames = [add_symbol_features(data[s], data['BTCUSDT']) for s in loaded]
    d = add_cross_sectional(pd.concat(frames, ignore_index=True))
    d = d[(d.qv24 >= EVENT_MIN_QV24)].replace([np.inf, -np.inf], np.nan)
    d = d.dropna(subset=['ret_1h','ret_4h','rs_1h','rs_4h','volume_ratio','taker_buy_ratio','rsi14','ema9','ema21'])
    if d.empty:
        raise RuntimeError('feature frame empty')

    times = sorted(d.ts.unique())
    t60 = times[int(len(times) * .60)]
    t80 = times[int(len(times) * .80)]
    d['split'] = np.where(d.ts < t60, 'DISCOVERY', np.where(d.ts < t80, 'CALIBRATION', 'TEST'))
    d['symbol_holdout'] = d.symbol.map(symbol_holdout)
    masks = setups(d)

    # Build per-symbol index maps once so barrier labels use each symbol's own next bar.
    grouped = {s: g.sort_values('ts').reset_index(drop=True) for s, g in d.groupby('symbol')}
    index_lookup = {}
    for s, g in grouped.items():
        index_lookup[s] = {pd.Timestamp(ts): i for i, ts in enumerate(g.ts)}

    hypotheses = []
    for setup_name, mask in masks.items():
        selected = d[mask.fillna(False)]
        for contract_name, contract in CONTRACTS.items():
            buckets = {'DISCOVERY': [], 'CALIBRATION': [], 'TEST': [], 'SYMBOL_HOLDOUT': []}
            for row in selected.itertuples(index=False):
                split = row.split
                hold = bool(row.symbol_holdout)
                if hold:
                    bucket = 'SYMBOL_HOLDOUT'
                else:
                    bucket = split
                # Discovery never sees holdout symbols; calibration/test remain chronological non-holdout.
                if hold and split == 'DISCOVERY':
                    bucket = 'SYMBOL_HOLDOUT'
                g = grouped[row.symbol]
                idx = index_lookup[row.symbol].get(pd.Timestamp(row.ts))
                if idx is None:
                    continue
                res = barrier_result(g, idx, contract)
                if res:
                    buckets[bucket].append(res)
            hypotheses.append({
                'setup': setup_name, 'contract': contract_name,
                'contractSpec': {**contract, 'roundTripCost': ROUND_TRIP_COST},
                'discovery': summarize(buckets['DISCOVERY']),
                'calibration': summarize(buckets['CALIBRATION']),
                'test': summarize(buckets['TEST']),
                'symbolHoldout': summarize(buckets['SYMBOL_HOLDOUT']),
            })

    bh(hypotheses)
    for r in hypotheses:
        r['calibrationPass'] = validation_pass(r['calibration'])
        r['testPass'] = validation_pass(r['test'])
        r['symbolHoldoutPass'] = validation_pass(r['symbolHoldout'])
        r['fullEvidencePass'] = bool(r['discoveryPass'] and r['calibrationPass'] and r['testPass'] and r['symbolHoldoutPass'])

    survivors = [r for r in hypotheses if r['fullEvidencePass']]
    survivors.sort(key=lambda r: (r['test']['meanNet'], r['symbolHoldout']['meanNet']), reverse=True)
    payload = {
        'engine': 'INTRADAY_BARRIER_EDGE_SCANNER_V1',
        'authorization': 'RESEARCH_ONLY', 'liveTrading': False, 'automaticPromotion': False,
        'interval': INTERVAL, 'days': DAYS,
        'universePolicy': {'maxSymbols': MAX_SYMBOLS, 'currentMinQuoteVolume': CURRENT_MIN_QV, 'eventPointInTimeMinQv24': EVENT_MIN_QV24},
        'symbolsRequested': syms, 'symbolsLoaded': loaded, 'failures': failures,
        'validationDesign': {
            'timeSplit': '60/20/20 chronological', 'symbolHoldout': 'sha256(symbol)%5==0',
            'sameBarTpSlResolution': 'SL_FIRST_PESSIMISTIC', 'entryTiming': 'NEXT_15M_OPEN',
            'fdr': {'method': 'Benjamini-Hochberg', 'q': FDR_Q},
            'discoveryGate': {'minEvents': MIN_DISCOVERY_EVENTS, 'meanNet': '>0', 'profitFactor': '>=1.15'},
            'validationGate': {'minEvents': MIN_VALIDATION_EVENTS, 'meanNet': '>0', 'profitFactor': '>=1.05'},
        },
        'hypothesisCount': len(hypotheses), 'fullEvidencePassCount': len(survivors),
        'survivors': survivors,
        'allHypotheses': hypotheses,
        'runtimeSeconds': round(time.time() - started, 2),
        'note': 'A PASS is research evidence only. It never enables or modifies live execution automatically.',
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding='utf-8')
    log(f"DONE hypotheses={len(hypotheses)} survivors={len(survivors)} runtime={payload['runtimeSeconds']}s")


if __name__ == '__main__':
    main()
