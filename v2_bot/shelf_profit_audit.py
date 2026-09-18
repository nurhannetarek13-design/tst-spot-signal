"""Zero-orders audit of the EXISTING frozen V2 grid, DCA and breakout engines.

No parameter search, optimizations, historical spread fabrication presented as truth,
portfolio pooling, exchange credentials, paper-ledger mutation, or live approval.
Historical signal only uses fully closed 15m/1h; next 15m OPEN proxies the
unavailable historical bid/ask. Recorded stop touches are NOT claimed fills.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx

from .binance_public import BinancePublicClient
from .crash_fvg_profit_audit import (START, END, PERIODS, SYMBOLS, BAR,
                                     ASSUMED_FULL_SPREAD, BUDGET, ms, validate_bars,
                                     proxy_book)
from .historical_replay import fetch_spot_15m, resample_15m
from .pionex_style import FEE, SLIPPAGE, MODES, Rules, new_state, step

SOURCE_PIN = '317ebf74b3da86e440e06afae5b222e3b1dc98d4'


def audit_one(rows: list[dict], hours: list[dict], hour_closes: list[int],
              symbol: str, mode: str, rules: Rules, start: str, end: str) -> dict:
    begin, finish = ((ms(day) - ms(START)) // BAR for day in (start, end))
    state = new_state(mode, symbol, BUDGET)
    peak, drawdown = BUDGET, 0.0
    fills, pnl, action_counts, stop_touches = [], [], Counter(), 0
    # No cross-period positions: each segment is a separate independent $50
    # experiment, with historical pre-period bars used for indicator warmup only.
    for i in range(max(begin, 220), finish - 1):
        cutoff = int(rows[i]['open_time']) + BAR - 1
        h_end = bisect.bisect_right(hour_closes, cutoff)
        if h_end < 55:
            continue
        fifteen = rows[max(0, i-119):i+1]
        hourly = hours[max(0, h_end-80):h_end]
        if len(fifteen) < 65 or len(hourly) < 55:
            continue
        # The last candle has closed. An order can only be assumed at the
        # next bar's OPEN, not at a favorable prior candle high/low/close.
        next_open = float(rows[i+1]['open'])
        book = proxy_book(next_open)
        before, prev_lots = state['realized_pnl'], [dict(x) for x in state['lots']]
        if prev_lots:
            if mode == 'trend_breakout':
                stop = prev_lots[0]['entry'] * .987
            elif mode == 'fixed_dca':
                stop = (sum(x['cost'] for x in prev_lots) /
                        sum(x['qty'] for x in prev_lots)) * .96
            else:
                stop = float(state['anchor']) * .93 if state['anchor'] else 0.
            if stop > 0 and rows[i]['low'] <= stop < book['bid']:
                stop_touches += 1  # NOT evidence of a fill. Current engine omits this.
        result = step(state, fifteen, hourly, book, rules)
        action = result['action']
        action_counts[action] += 1
        if action in ('paper_buy', 'paper_sell'):
            fills.append({'signal_bar':int(rows[i]['open_time']),
                'proxy_entry_or_exit_bar':int(rows[i+1]['open_time']),
                'action':action,'next_open':next_open,
                'delta_realized_usdt':round(state['realized_pnl']-before,8),
                'fees_paid_total_usdt':round(state['fees_paid'],8)})
        if state['realized_pnl'] != before:
            pnl.append(float(state['realized_pnl']-before))
        # This asserts actual ledger accounting instead of using peak realized
        # returns to hide underwater inventory.
        conservation = state['cash'] + sum(x['cost'] for x in state['lots'])
        if not math.isclose(conservation, BUDGET+state['realized_pnl'], abs_tol=1e-5):
            raise RuntimeError(f'ACCOUNTING_MISMATCH_{symbol}_{mode}_{i}')
        mark = state['cash'] + sum(x['qty'] * book['bid'] for x in state['lots'])
        peak = max(peak, mark)
        drawdown = max(drawdown, peak-mark)
    # End mark is NOT a realized liquidation. Include estimated costs separately.
    final_bid = float(rows[finish-1]['close']) * (1-ASSUMED_FULL_SPREAD/2)
    qty = sum(x['qty'] for x in state['lots'])
    mark = state['cash'] + qty*final_bid
    est_liquidation = (state['cash'] + qty*final_bid*(1-SLIPPAGE)*(1-FEE))
    drawdown = max(drawdown, peak-mark)
    wins, losses = [x for x in pnl if x>0], [x for x in pnl if x<0]
    gross_loss = -sum(losses)
    pf = sum(wins)/gross_loss if gross_loss else None
    result = {'symbol':symbol,'mode':mode,'start_utc':start,'end_exclusive_utc':end,
        'virtual_buys':action_counts['paper_buy'],
        'closed_trades':len(pnl),'wins':len(wins),'losses':len(losses),
        'win_rate':round(len(wins)/len(pnl),6) if pnl else None,
        'profit_factor':round(pf,6) if pf is not None else None,
        'expectancy_usdt':round(sum(pnl)/len(pnl),7) if pnl else None,
        'realized_net_usdt':round(state['realized_pnl'],7),
        'marked_net_usdt':round(mark-BUDGET,7),
        'estimated_liquidation_net_usdt_not_a_fill':round(est_liquidation-BUDGET,7),
        'fees_paid_usdt':round(state['fees_paid'],7),
        'max_mark_drawdown_usdt':round(drawdown,7),
        'intrabar_stop_touches_not_executed_or_verifiable':stop_touches,
        'halted':bool(state['halted']),'open_lots':len(state['lots']),
        'proxy_book_not_historical_l1':True,
        'actions':dict(action_counts),'fills':fills}
    # This is a fixed, intentionally strict research gate, not proof of profit.
    # Require BOTH unseen segments to pass; only Q4 is a descriptive discovery.
    result['descriptive_sample_gate'] = bool(
        len(pnl)>=30 and len(wins)>0 and len(losses)>0 and pf is not None and pf>1.2
        and mark>BUDGET and state['realized_pnl']>0 and drawdown<2.
        and stop_touches==0 and not state['halted'] and not state['lots'])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Audit 9 EXISTING strategies, never trade')
    parser.add_argument('--output',default='shelf_profit_audit.json')
    args=parser.parse_args()
    data, hashes, rules_by_symbol = {}, {}, {}
    with httpx.Client(timeout=35, headers={'User-Agent':'tst-v2-shelf-profit-audit/1.0'}) as client:
        for symbol in SYMBOLS:
            bars=fetch_spot_15m(symbol,start_ms=ms(START),end_ms=ms(END)-1,client=client)
            hashes[symbol]=validate_bars(bars,symbol)
            data[symbol]=bars
            print('VERIFIED_HISTORY',symbol,len(bars),hashes[symbol],flush=True)
    with closing(BinancePublicClient()) as market:
        for symbol in SYMBOLS:
            rules_by_symbol[symbol]=Rules.from_exchange_info(market.exchange_info(symbol),symbol)
    hourly={s:resample_15m(rows,4) for s,rows in data.items()}
    closes={s:[int(r['close_time']) for r in hour] for s,hour in hourly.items()}
    results=[]
    for start,end,period in PERIODS:
        for symbol in SYMBOLS:
            for mode in MODES:
                row=audit_one(data[symbol],hourly[symbol],closes[symbol],symbol,
                              mode,rules_by_symbol[symbol],start,end)
                row['period']=period
                results.append(row)
                print('SHELF_RESULT',json.dumps({k:row[k] for k in (
                    'period','symbol','mode','virtual_buys','closed_trades','wins',
                    'losses','realized_net_usdt','marked_net_usdt','profit_factor',
                    'max_mark_drawdown_usdt','intrabar_stop_touches_not_executed_or_verifiable',
                    'halted','open_lots','descriptive_sample_gate')},sort_keys=True),flush=True)
    gates={mode: all(any(x['mode']==mode and x['symbol']==symbol and x['period']==period
                        and x['descriptive_sample_gate'] for x in results)
                      for period in ('2026_JAN_APR_HOLDOUT','2026_MAY_SEP_HOLDOUT')
                      for symbol in SYMBOLS)
           for mode in MODES}
    report={'status':'DIAGNOSTIC_ONLY_NOT_PROVEN_NOT_LIVE_READY',
        'frozen_source_commit':SOURCE_PIN,
        'period':{'start_utc':START,'end_exclusive_utc':END},
        'independent_50_usdt_scenarios_not_additive':True,
        'assumed_spread_bps':ASSUMED_FULL_SPREAD*10000,
        'fee_per_side':FEE,'slippage_per_side':SLIPPAGE,
        'historical_l1_unavailable':True,'current_symbol_filters_proxy':True,
        'stop_rule_not_native_exchange_protection':True,
        'real_orders':0,'live_trading':False,'data_sha256':hashes,
        'results':results,'all_symbols_both_holdouts_pass_gate':gates,
        'any_strategy_qualified_for_live':False}
    Path(args.output).write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False))
    lines=['# Frozen V2 strategy shelf: REAL historical candles, estimated fills only',
           f'{START} to {END} exclusive UTC; fees and slippage included. Historical bid/ask NOT available.',
           '**No profit guarantee or live approval. Accounts $50 each independently, NOT a combined bankroll.**',
           '| Period | Pair | Mode | Buys | Closed | W/L | Realized USDT | Marked USDT | PF | Drawdown | Missed intrabar stop touches | Halted |',
           '|---|---|---|---:|---:|---|---:|---:|---:|---:|---:|---|']
    for r in results:
        lines.append(f"| {r['period']} | {r['symbol']} | {r['mode']} | {r['virtual_buys']} | {r['closed_trades']} | {r['wins']}/{r['losses']} | {r['realized_net_usdt']:+.4f} | {r['marked_net_usdt']:+.4f} | {r['profit_factor']} | {r['max_mark_drawdown_usdt']:.4f} | {r['intrabar_stop_touches_not_executed_or_verifiable']} | {r['halted']} |")
    lines.extend(['','**Holdout descriptive gates (all three symbols in both holdouts, not live approval):** '+str(gates),
                  '**Conclusions are proxy-dependent: next bar open and synthetic 3bps spread. Intrabar touches never assumed as executed orders; real exchange-native stops not installed.**'])
    Path(args.output).with_suffix('.md').write_text('\n'.join(lines)+'\n')
    print('SHELF_AUDIT_FINISHED REAL_ORDERS=0 GATES='+json.dumps(gates,sort_keys=True),flush=True)


if __name__=='__main__':
    main()
