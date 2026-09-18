"""Diagnostic historical replay of exact frozen crash and FVG forward-paper engines.

Three predetermined, nonoverlapping segments (2025-09-01 to 2026-09-17 UTC),
BTC/ETH/SOL, fully contiguous public Binance 15m candles. Decision uses only
closed bar i and the next bar i+1 OPEN as an assumed book quote. Historical L1
is unavailable, so the assumed spread cannot prove executable profitability.
No real orders, private credentials, live authorization, or optimizer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import crash_reversal_retest as base
from . import fvg_ab_paper as fvg
from .binance_public import BinancePublicClient
from .fvg_detector import detect_crash_fvg_retest
from .historical_replay import fetch_spot_15m
from .pionex_style import FEE, SLIPPAGE, Rules

BAR = 900_000
START, END = '2025-09-01', '2026-09-17'
PERIODS = (('2025-09-01', '2026-01-01', '2025_Q4'),
           ('2026-01-01', '2026-05-01', '2026_JAN_APR_HOLDOUT'),
           ('2026-05-01', '2026-09-17', '2026_MAY_SEP_HOLDOUT'))
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
ASSUMED_FULL_SPREAD = .0003  # 3bps, NOT a historical measurement.
BUDGET = 50.0


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def validate_bars(rows: list[dict], symbol: str) -> str:
    expected = (ms(END) - ms(START)) // BAR
    if (len(rows) != expected or not rows or int(rows[0]['open_time']) != ms(START)
            or int(rows[-1]['open_time']) != ms(END) - BAR):
        raise RuntimeError(f'INCOMPLETE_HISTORY_{symbol}_{len(rows)}_EXPECTED_{expected}')
    digest = hashlib.sha256()
    for idx, row in enumerate(rows):
        if int(row['open_time']) != ms(START) + idx * BAR:
            raise RuntimeError(f'MISSING_OR_DUPLICATE_BAR_{symbol}_{idx}')
        if not (0 < row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']
                and row['quote_volume'] >= 0
                and all(math.isfinite(float(row[k])) for k in
                        ('open_time','open','high','low','close','quote_volume'))):
            raise RuntimeError(f'INVALID_OHLC_{symbol}_{idx}')
        digest.update(json.dumps([int(row['open_time']), row['open'], row['high'],
            row['low'], row['close'], row['quote_volume']], separators=(',', ':')).encode())
    return digest.hexdigest()


def proxy_book(price: float) -> dict[str,float]:
    return {'bid': price * (1 - ASSUMED_FULL_SPREAD / 2),
            'ask': price * (1 + ASSUMED_FULL_SPREAD / 2)}


def replay(rows: list[dict], symbol: str, rules: Rules, start: str,
           end: str, variant: str) -> dict:
    begin, finish = ((ms(day) - ms(START)) // BAR for day in (start, end))
    engine = base if variant == 'baseline' else fvg
    state = engine.new_state(symbol, BUDGET)
    peak, maximum_dd = BUDGET, 0.0
    closed = []
    actions = Counter()
    raw_signals = 0
    fills_and_stops = []
    # Signal candle i is closed. Next candle i+1 OPEN is known only AFTER i closes;
    # never use candle i+1 high/low/close in the signal or entry decision.
    for idx in range(max(begin, 79), finish - 1):
        candles = rows[idx-79:idx+1]
        next_open = float(rows[idx+1]['open'])
        book = proxy_book(next_open)
        raw = (base.detect(candles, book['ask'], book['bid']) if variant == 'baseline'
               else detect_crash_fvg_retest(candles, ask=book['ask'], bid=book['bid']))
        raw_signals += raw is not None
        before = float(state['realized_pnl'])
        trade_count = int(state['trades'])
        outcome = engine.step(state, candles, book, rules)
        engine.validate(state, symbol=symbol, budget=BUDGET)
        action = outcome['action']
        actions[action] += 1
        if state['trades'] > trade_count or action.startswith('intrabar_stop_touch'):
            fills_and_stops.append({'closed_signal_bar_ms':int(rows[idx]['open_time']),
                'next_open_bar_ms':int(rows[idx+1]['open_time']),
                'action':action,'next_open_proxy':next_open,
                'realized_trade_pnl_delta_usdt':round(state['realized_pnl']-before,8),
                'remaining_position_qty':sum(lot['qty'] for lot in state['lots'])})
        if state['realized_pnl'] != before:
            closed.append(float(state['realized_pnl'] - before))
        equity = float(state['cash']) + sum(lot['qty'] * book['bid'] for lot in state['lots'])
        peak = max(peak, equity)
        maximum_dd = max(maximum_dd, peak-equity)
    # Open positions remain open, not fabricated as end-of-window sales.
    final_bid = float(rows[finish-1]['close']) * (1 - ASSUMED_FULL_SPREAD/2)
    end_equity = float(state['cash']) + sum(lot['qty']*final_bid for lot in state['lots'])
    maximum_dd = max(maximum_dd, peak-end_equity)
    wins, losses = [p for p in closed if p > 0], [p for p in closed if p < 0]
    loss_abs = -sum(losses)
    unresolved = bool(state['lots'] or state['halted'])
    return {'symbol':symbol,'variant':variant,'start_utc':start,'end_exclusive_utc':end,
        'closed_bars_processed':max(0,finish-max(begin,79)-1),
        'pattern_hits':raw_signals,'virtual_buys':actions['paper_buy'],
        'closed_trades':len(closed),'wins':len(wins),'losses':len(losses),
        'win_rate':round(len(wins)/len(closed),6) if closed else None,
        'profit_factor':round(sum(wins)/loss_abs,6) if loss_abs else None,
        'expectancy_usdt':round(sum(closed)/len(closed),7) if closed else None,
        'realized_net_pnl_usdt':round(state['realized_pnl'],7),
        'marked_net_pnl_usdt':round(end_equity-BUDGET,7),
        'virtual_equity_usdt':round(end_equity,7),
        'fees_paid_usdt':round(state['fees_paid'],7),
        'max_mark_to_market_drawdown_usdt':round(maximum_dd,7),
        'open_position':bool(state['lots']),'halted':bool(state['halted']),
        'unresolved_or_halted':unresolved,
        'ambiguous_stop_halts':actions['intrabar_stop_touch_halted_no_fabricated_fill'],
        'actions':dict(actions),'fills_and_stop_audit':fills_and_stops,
        'sample_result':('UNRESOLVED_NO_PROFIT_CLAIM' if unresolved else
           'NO_CLOSED_TRADES' if not closed else
           'SAMPLE_MARKED_NET_POSITIVE_NOT_PROOF' if end_equity > BUDGET else 'SAMPLE_NET_NOT_POSITIVE')}


def main() -> None:
    parser = argparse.ArgumentParser(description='Zero-orders 381-day historical audit of frozen FVG/crash paper')
    parser.add_argument('--output', default='crash_fvg_profit_audit.json')
    args = parser.parse_args()
    if sum(ms(b)-ms(a) for a,b,_ in PERIODS) != ms(END)-ms(START):
        raise RuntimeError('PERIODS_DO_NOT_COVER_HISTORY')
    data, hashes, exchange = {}, {}, {}
    with httpx.Client(timeout=35, headers={'User-Agent':'tst-crash-fvg-profit-audit/1.0'}) as client:
        for symbol in SYMBOLS:
            rows = fetch_spot_15m(symbol,start_ms=ms(START),end_ms=ms(END)-1,client=client)
            hashes[symbol] = validate_bars(rows,symbol)
            data[symbol] = rows
            print(f'HISTORY_PASS symbol={symbol} bars={len(rows)} sha256={hashes[symbol]}',flush=True)
    with closing(BinancePublicClient()) as market:
        for symbol in SYMBOLS:
            exchange[symbol] = Rules.from_exchange_info(market.exchange_info(symbol),symbol)
    results = []
    for start,end,label in PERIODS:
        for symbol in SYMBOLS:
            for variant in ('baseline','fvg'):
                result = replay(data[symbol],symbol,exchange[symbol],start,end,variant)
                result['period'] = label
                results.append(result)
                print('AUDIT_RESULT '+json.dumps({key:result[key] for key in
                    ('period','symbol','variant','pattern_hits','virtual_buys','closed_trades',
                     'realized_net_pnl_usdt','marked_net_pnl_usdt','profit_factor',
                     'max_mark_to_market_drawdown_usdt','unresolved_or_halted','sample_result')},
                    sort_keys=True,allow_nan=False),flush=True)
    report={'verdict':'HISTORICAL_DIAGNOSTIC_ONLY_NO_PROFIT_GUARANTEE_OR_LIVE_APPROVAL',
      'strategy_source_commit':'317ebf74b3da86e440e06afae5b222e3b1dc98d4',
      'period':{'start_utc':START,'end_exclusive_utc':END},
      'symbols':list(SYMBOLS),'data_sha256':hashes,
      'fees_each_side':FEE,'slippage_each_side':SLIPPAGE,
      'historical_spread':'ASSUMED_3_BPS_NO_HISTORICAL_L1',
      'entry':'NEXT_15M_BAR_OPEN_PROXY_NOT_VERIFIED_HISTORICAL_BOOK',
      'stop':'EXACT_PAPER_ENGINE_HALTS_IF_TOUCHED_INTRABAR_BUT_UNVERIFIABLE',
      'historical_filters':'CURRENT_EXCHANGE_RULES_PROXY',
      'capital':'SIX_INDEPENDENT_ALTERNATIVE_VIRTUAL_50_USDT_ACCOUNTS_NOT_ADDITIVE',
      'real_orders':0,'live_trading':False,'results':results}
    Path(args.output).write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False))
    lines=['# Crash Retest vs FVG: historical profitability audit',
      f'{START} to {END} exclusive UTC, full verified closed Spot 15-minute candles, BTC ETH SOL.',
      '**Historical bid/ask absent: synthetic 3bps spread; next-open proxy; no installed exchange stops. Evidence cannot guarantee a real or future profit. All six $50 balances are alternative simulations, not a portfolio.**',
      '| Period | Pair | Variant | Signals | Buys | Closed | Marked net USDT | Realized net USDT | PF | Max DD USDT | Unresolved |',
      '|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for row in results:
        pf = str(row['profit_factor']) if row['profit_factor'] is not None else 'N/A'
        lines.append(f"| {row['period']} | {row['symbol']} | {row['variant']} | {row['pattern_hits']} | {row['virtual_buys']} | {row['closed_trades']} | {row['marked_net_pnl_usdt']:+.5f} | {row['realized_net_pnl_usdt']:+.5f} | {pf} | {row['max_mark_to_market_drawdown_usdt']:.5f} | {row['unresolved_or_halted']} |")
    lines.append('**No completed losing trades means PF cannot be estimated. Unresolved stop halts cannot be counted as successful exits. No real orders.**')
    Path(args.output).with_suffix('.md').write_text('\n'.join(lines)+'\n')
    print('HISTORICAL_PROFITABILITY_AUDIT_FINISHED; REAL_ORDERS=0',flush=True)


if __name__=='__main__':
    main()
