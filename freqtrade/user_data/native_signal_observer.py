#!/usr/bin/env python3
"""Original-source, read-only Freqtrade 15m observer.

Run in the Freqtrade container. Fetches PUBLIC, completed Binance Spot candles;
invokes each installed strategy's actual indicator/entry/exit methods, emits a
JSON diagnostic and DOES NOT open positions, infer performance, send alerts,
read credentials or call exchange private APIs. Never import into paper wallet
until full source exit/protection parity and evidence are independently checked.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'strategies'
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'SUIUSDT', 'NEARUSDT', 'FILUSDT', 'LINKUSDT', 'AVAXUSDT')
STRATEGIES = (
    ('TST_ALLIGATOR_SMC_V2', 'AdaptiveRegimeStrategy', 'AdaptiveRegimeStrategy'),
    ('BASTION_JOAT_SPOT_V2', 'BastionJOATSpotV2', 'BastionJOATSpotV2'),
)


def finished_klines(raw, now_ms):
    """Include only bars whose exchange close timestamp is strictly in the past."""
    result = []
    previous = -1
    for item in raw:
        if len(item) < 7:
            raise ValueError('BAD_KLINE_FORMAT')
        start, end = int(item[0]), int(item[6])
        if start <= previous:
            raise ValueError('NON_MONOTONIC_CANDLES')
        previous = start
        if end >= now_ms:
            continue
        numbers = [float(item[i]) for i in (1, 2, 3, 4, 5)]
        if not all(math.isfinite(x) for x in numbers) or not (
                numbers[0] > 0 and numbers[1] >= max(numbers[0], numbers[3])
                and numbers[2] <= min(numbers[0], numbers[3]) and numbers[2] > 0
                and numbers[4] >= 0):
            raise ValueError('INVALID_CANDLE_VALUES')
        result.append(dict(date=datetime.fromtimestamp(start / 1000, tz=timezone.utc),
                           open=numbers[0], high=numbers[1], low=numbers[2],
                           close=numbers[3], volume=numbers[4], open_time=start))
    return result


def public_klines(symbol, limit=500):
    if symbol not in SYMBOLS or not 360 <= limit <= 1000:
        raise ValueError('UNAPPROVED_MARKET_OR_HISTORY')
    req = urllib.request.Request(
        f'https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=15m&limit={limit}',
        headers={'User-Agent': 'tst-freqtrade-observer/1.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = json.load(response)
    return finished_klines(raw, time.time() * 1000)


def safe_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate(symbol, rows, source_id, module_name, class_name):
    """Execute the actual class methods; fail closed if a dependency is missing."""
    import importlib
    import pandas as pd
    if len(rows) < 360:
        raise RuntimeError('INSUFFICIENT_FINISHED_15M_HISTORY')
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    klass = getattr(importlib.import_module(module_name), class_name)
    if getattr(klass, 'can_short', True) is not False or klass.timeframe != '15m':
        raise RuntimeError('NOT_SPOT_LONG_15M')
    # No Freqtrade bot object or credentials. These source methods access only
    # class parameters and the passed DataFrame, not a trading DataProvider.
    strategy = object.__new__(klass)
    frame = pd.DataFrame(rows)
    annotated = strategy.populate_indicators(frame, {'pair': symbol.replace('USDT', '/USDT')})
    entered = strategy.populate_entry_trend(annotated, {'pair': symbol.replace('USDT', '/USDT')})
    exited = strategy.populate_exit_trend(entered, {'pair': symbol.replace('USDT', '/USDT')})
    last = exited.iloc[-1]
    result = dict(strategy=source_id, symbol=symbol, mode='OBSERVE_ONLY',
                  source=f'{module_name}.py', bar_time=int(last['open_time']),
                  entry=bool(last.get('enter_long', 0) == 1),
                  exit_signal=bool(last.get('exit_long', 0) == 1),
                  close=safe_number(last.get('close')), atr=safe_number(last.get('atr')),
                  entry_tag=str(last.get('enter_tag') or '') if last.get('enter_long', 0) == 1 else '',
                  exit_tag=str(last.get('exit_tag') or '') if last.get('exit_long', 0) == 1 else '')
    if source_id == 'TST_ALLIGATOR_SMC_V2':
        result['structural_risk_fraction'] = safe_number(last.get('structure_risk_pct'))
        result['demand_low'] = safe_number(last.get('demand_low'))
        result['sweep_low'] = safe_number(last.get('sweep_low'))
        # The native class has dynamic ROI, trailing and protections not
        # represented by our fixed-stop paper wallet: observe only.
        result['paper_entry_allowed'] = False
        result['block_reason'] = 'NATIVE_ROI_TRAILING_PROTECTIONS_NOT_PARITY_TESTED'
    else:
        result['paper_entry_allowed'] = False
        result['block_reason'] = 'JOAT_CUSTOM_TRAIL_AND_NY_EXIT_NOT_PARITY_TESTED'
    return result


def main():
    # This is an independent, read-only diagnostic. Explicitly disallow
    # environment switches that might imply real execution.
    if os.getenv('LIVE_TRADING', '').lower() in ('1', 'true', 'yes'):
        raise RuntimeError('LIVE_MODE_FORBIDDEN')
    report = dict(mode='OBSERVE_ONLY', generated_at=datetime.now(timezone.utc).isoformat(),
                  native_strategies=[x[0] for x in STRATEGIES], rows=[], errors=[])
    for symbol in SYMBOLS:
        try:
            rows = public_klines(symbol)
            for strategy_id, module, name in STRATEGIES:
                try:
                    report['rows'].append(evaluate(symbol, rows, strategy_id, module, name))
                except Exception as exc:
                    report['errors'].append(dict(symbol=symbol, strategy=strategy_id,
                                                 reason=type(exc).__name__, detail=str(exc)[:160]))
        except Exception as exc:
            report['errors'].append(dict(symbol=symbol, reason=type(exc).__name__, detail=str(exc)[:160]))
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    # Even a clean no-entry result is diagnostic, not strategy approval.
    if not report['rows']:
        raise RuntimeError('NO_NATIVE_STRATEGY_EVALUATIONS')


if __name__ == '__main__':
    main()
