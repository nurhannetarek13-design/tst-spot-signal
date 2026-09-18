"""Historical diagnostic of the EXACT frozen forward-paper decision functions.

No exchange orders, optimizer, strategy threshold changes, fabricated L1 quotes,
or profit approval. 2025-09-01..2026-09-17 UTC, full closed 15m bars,
BTC/ETH/SOL; three predeclared non-overlapping periods. Entry decision is
based on bar i and a proxy quote at NEXT bar i+1 open. This model does NOT
recreate historical order books, real stop orders, downtime, latency, or
exchange fill guarantees; it therefore can reject bad evidence, never prove
real-world profitability. Paper engine HALTS on an intrabar touched stop
rather than inventing a fill; these positions remain unresolved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import crash_reversal_retest as base
from . import fvg_ab_paper as challenger
from .fvg_detector import detect_crash_fvg_retest
from .historical_replay import fetch_spot_15m
from .pionex_style import FEE, SLIPPAGE, Rules

BAR = 900_000
START = '2025-09-01'
END_EXCLUSIVE = '2026-09-17'
PERIODS = (('2025-09-01', '2026-01-01', '2025_Q4'),
           ('2026-01-01', '2026-05-01', '2026_JAN_APR_HOLDOUT'),
           ('2026-05-01', '2026-09-17', '2026_MAY_SEP_HOLDOUT'))
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
# Hypothetical full spread 3 bps: no historical L1 snapshots were downloaded.
ASSUMED_FULL_SPREAD = .0003
BUDGET = 50.0


def ms(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


def validate_bars(rows: list[dict], symbol: str) -> str:
    start, end = ms(START), ms(END_EXCLUSIVE)
    expected = (end - start) // BAR
    if len(rows) != expected or int(rows[0]['open_time']) != start or int(rows[-1]['open_time']) != end - BAR:
        raise RuntimeError(f'INCOMPLETE_HISTORY_{symbol}_{len(rows)}_expected_{expected}')
    digest = hashlib.sha256()
    for i, row in enumerate(rows):
        if int(row['open_time']) != start + i * BAR:
            raise RuntimeError(f'HISTORY_GAP_OR_DUPLICATE_{symbol}_{i}')
        if not (0 < row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']
                and row['quote_volume'] >= 0 and all(math.isfinite(float(row[k])) for k in
                    ('open_time','open','high','low','close','quote_volume'))):
            raise RuntimeError(f'BAD_OHLC_{symbol}_{i}')
        digest.update(json.dumps([int(row['open_time']), row['open'], row['high'],
             row['low'], row['close'], row['quote_volume']], separators=(',', ':')).encode())
    return digest.hexdigest()


def hypothetical_book(next_open: float) -> dict[str, float]:
    return {'bid': next_open * (1 - ASSUMED_FULL_SPREAD / 2),
            'ask': next_open * (1 + ASSUMED_FULL_SPREAD / 2)}


def period_replay(rows: list[dict], symbol: str, rules: Rules, first: str,
                  last: str, variant: str) -> dict:
    start, end = ms(first), ms(last)
    start_idx = (start - ms(START)) // BAR
    end_idx = (end - ms(START)) // BAR
    engine = base if variant == 'baseline' else challenger
    state = engine.new_state(symbol, BUDGET)
    peak = BUDGET
    max_dd = 0.0
    outcomes: list[float] = []
    actions = Counter()
    eligible_pattern_count = 0
    audit = []
    # Row i is fully closed before hypothetical entry quote at row i+1 open.
    # No future high/low is accessed by an entry; the original step() is reused.
    for i in range(max(start_idx, 79), end_idx - 1):
        candles = rows[i-79:i+1]
        next_open = float(rows[i+1]['open'])
        book = hypothetical_book(next_open)
        detected = (base.detect(candles, book['ask'], book['bid']) if variant == 'baseline'
                    else detect_crash_fvg_retest(candles, ask=book['ask'], bid=book['bid']))
        if detected:
            eligible_pattern_count += 1
        previous_realized = float(state['realized_pnl'])
        previous_trades = state['trades']
        signal = engine.step(state, candles, book, rules)
        engine.validate(state, symbol=symbol, budget=BUDGET)
        action = signal['action']
        actions[action] += 1
        if state['trades'] > previous_trades or action.startswith('intrabar_stop_touch'):
            audit.append({'signal_bar': int(rows[i]['open_time']),
                'next_open': int(rows[i+1]['open_time']), 'action': action,
                'entry_or_exit_proxy': next_open,
                'realized_delta_after_fees_usdt': round(state['realized_pnl'] - previous_realized, 8),
                'outstanding_qty': sum(lot['qty'] for lot in state['lots'])})
        if state['realized_pnl'] != previous_realized:
            outcomes.append(float(state['realized_pnl'] - previous_realized))
        equity = float(state['cash']) + sum(lot['qty'] * book['bid'] for lot in state['lots'])
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    # Last bar is marked to market; no fictional forced liquidation at end.
    final_bid = rows[end_idx - 1]['close'] * (1 - ASSUMED_FULL_SPREAD / 2)
    final_equity = float(state['cash']) + sum(lot['qty'] * final_bid for lot in state['lots'])
    max_dd = max(max_dd, peak - final_equity)
    wins = [p for p in outcomes if p > 0]
    losses = [p for p in outcomes if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    unresolved = bool(state['lots'] or state['halted'])
    return {'period_start_utc': first, 'period_end_exclusive_utc': last,
        'symbol': symbol, 'variant': variant, 'closed_bars_processed': max(0, end_idx-max(start_idx,79)-1),
        'pattern_hits': eligible_pattern_count, 'virtual_buys': actions['paper_buy'],
        'closed_trades': len(outcomes), 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(len(wins)/len(outcomes), 6) if outcomes else None,
        'profit_factor': round(gross_profit/gross_loss, 6) if gross_loss > 0 else None,
        'expectancy_usdt_per_closed_trade': round(sum(outcomes)/len(outcomes), 7) if outcomes else None,
        'realized_net_pnl_usdt': round(state['realized_pnl'], 7),
        'marked_net_pnl_usdt': round(final_equity - BUDGET, 7),
        'virtual_equity_usdt': round(final_equity, 7),
        'fees_paid_usdt': round(state['fees_paid'], 7),
        'max_mark_to_market_drawdown_usdt': round(max_dd, 7),
        'open_position_at_end': bool(state['lots']), 'halted': bool(state['halted']),
        'unresolved_or_halted': unresolved,
        'ambiguous_touched_stop_halts': actions['intrabar_stop_touch_halted_no_fabricated_fill'],
        'actions': dict(actions), 'trade_and_stop_audit': audit,
        'profitability_status': ('NOT_ESTABLISHED_UNRESOLVED' if unresolved else
            'NO_CLOSED_TRADES' if not outcomes else
            'SAMPLE_NET_POSITIVE_NOT_PROOF' if final_equity > BUDGET else 'SAMPLE_NOT_PROFITABLE')}


def main() -> None:
    parser = argparse.ArgumentParser(description='No-orders historical audit of frozen A/B Spot paper strategies')
    parser.add_argument('--output', default='crash_fvg_profit_audit.json')
    args = parser.parse_args()
    assert sum(ms(b)-ms(a) for a,b,_ in PERIODS) == ms(END_EXCLUSIVE)-ms(START)
    data: dict[str, list[dict]] = {}
    hashes: dict[str,str] = {}
    rules_map: dict[str, Rules] = {}
    with httpx.Client(timeout=35, headers={'User-Agent':'tst-crash-fvg-profit-audit/1.0'}) as client:
        # Public historical candles only; zero authenticated access or trading.
        for symbol in SYMBOLS:
            rows = fetch_spot_15m(symbol, start_ms=ms(START),
                                    end_ms=ms(END_EXCLUSIVE)-1, client=client)
            hashes[symbol] = validate_bars(rows, symbol)
            data[symbol] = rows
            print(f'HISTORY_PASS {symbol} candles={len(rows)} sha256={hashes[symbol]}', flush=True)
        from .binance_public import BinancePublicClient
        with BinancePublicClient() as market:
            for symbol in SYMBOLS:
                rules_map[symbol] = Rules.from_exchange_info(market.exchange_info(symbol), symbol)
    results = []
    for a,b,label in PERIODS:
        for symbol in SYMBOLS:
            for variant in ('baseline','fvg'):
                result = period_replay(data[symbol], symbol, rules_map[symbol], a,b,variant)
                result['period'] = label
                results.append(result)
                print('AUDIT_RESULT ' + json.dumps({k:result[k] for k in
                    ('period','symbol','variant','pattern_hits','virtual_buys','closed_trades',
                     'realized_net_pnl_usdt','marked_net_pnl_usdt','profit_factor',
                     'max_mark_to_market_drawdown_usdt','unresolved_or_halted','profitability_status')},
                    sort_keys=True, allow_nan=False), flush=True)
    report = {'verdict':'HISTORICAL_DIAGNOSTIC_ONLY_NOT_FORWARD_OR_LIVE_PROFIT_PROOF',
        'source_version':'317ebf74b3da86e440e06afae5b222e3b1dc98d4',
        'market':'Binance public Spot 15m complete closed candles',
        'period':{'start_utc':START,'end_exclusive_utc':END_EXCLUSIVE},
        'symbols':list(SYMBOLS), 'candles_sha256':hashes,
        'fee_each_side':FEE,'slippage_each_side':SLIPPAGE,
        'historical_l1_quote':'UNAVAILABLE_ASSUMED_3_BPS_FULL_SPREAD_AT_NEXT_BAR_OPEN',
        'next_open_proxy':'NOT_HISTORICAL_EXECUTED_FILL',
        'stop_behavior':'exact_forward_paper_step_halts_on_missed_intrabar_stop_no_fabricated_exit',
        'max_loss_cap':'observed_paper_threshold_not_guaranteed_in_gaps',
        'current_exchange_filters_applied_historically':True,
        'each_50_usdt_account_is_independent_not_a_funded_300_usdt_portfolio':True,
        'real_orders':0,'live_trading':False, 'results':results}
    Path(args.output).write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False))
    lines=['# Frozen crash retest vs FVG: independent historical profitability audit',
           f'{START} through {END_EXCLUSIVE} exclusive UTC; complete public 15m candles; BTC/ETH/SOL.',
           '**Diagnostic only. Historical L1 missing: assumed 3-bps full spread and next-bar-open proxy. No real stops installed, no live orders, independent hypothetical $50 accounts.**',
           '| Period | Pair | Variant | Pattern hits | Buys | Closed | Net marked USDT | Realized net USDT | PF | Max drawdown | Unresolved/halted |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for r in results:
        lines.append('| {period} | {symbol} | {variant} | {pattern_hits} | {virtual_buys} | {closed_trades} | {marked_net_pnl_usdt:+.5f} | {realized_net_pnl_usdt:+.5f} | {pf} | {max_mark_to_market_drawdown_usdt:.5f} | {unresolved_or_halted} |'.format(
            **r,pf=r['profit_factor'] if r['profit_factor'] is not None else 'N/A'))
    lines.append('**No sample can guarantee future profitability. PF is undefined without closed losing trades. Unresolved touched stops invalidate positive-profit claims. No leverage or exchange orders.**')
    Path(args.output).with_suffix('.md').write_text('\n'.join(lines)+'\n')
    print('HISTORICAL_AUDIT_COMPLETED; REPORT=' + args.output + '; REAL_ORDERS=0',flush=True)


if __name__ == '__main__':
    main()
