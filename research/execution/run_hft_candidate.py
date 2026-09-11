#!/usr/bin/env python3
"""Replay a frozen candidate order plan through hftbacktest.

The candidate plan is created by the Primary Edge Lab and must be frozen BEFORE
execution validation. Each item is a long-only intended limit entry plus a fixed
exit timestamp. Entry fills are queue-sensitive; exits use a marketable IOC limit
at the observed best bid to close only the quantity actually filled.

This runner emits measured execution metrics. It never creates candidate signals.
"""
import argparse, json, math
from pathlib import Path

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, IOC, LIMIT, FILLED, PARTIALLY_FILLED

ACTIVE_OR_FILLED = (FILLED, PARTIALLY_FILLED)


def elapsed_to(hbt, target_ns):
    now = int(hbt.current_timestamp)
    if target_ns <= now:
        return True
    return hbt.elapse(int(target_ns - now)) == 0


def mk_asset(data_file, tick, lot, queue, latency_ns, fee):
    a = (BacktestAsset()
         .data([data_file])
         .linear_asset(1.0)
         .constant_order_latency(latency_ns, latency_ns)
         .partial_fill_exchange()
         .trading_value_fee_model(fee, fee)
         .tick_size(tick)
         .lot_size(lot))
    if queue == 'RISK_AVERSE':
        a = a.risk_adverse_queue_model()
    elif queue.startswith('PROB_QUEUE_N'):
        n = float(queue.rsplit('N', 1)[1])
        a = a.power_prob_queue_model(n)
    else:
        raise ValueError(f'unsupported queue model: {queue}')
    return a


def profit_factor(rets):
    wins = sum(x for x in rets if x > 0)
    losses = -sum(x for x in rets if x < 0)
    if losses == 0:
        return 999.0 if wins > 0 else 0.0
    return wins / losses


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data', required=True)
    p.add_argument('--plan', required=True)
    p.add_argument('--symbol', required=True)
    p.add_argument('--feed', choices=['L2_ONLY','L2_PLUS_BOOKTICKER'], required=True)
    p.add_argument('--queue', choices=['PROB_QUEUE_N1','PROB_QUEUE_N2','PROB_QUEUE_N3','RISK_AVERSE'], required=True)
    p.add_argument('--latency-ns', type=int, required=True)
    p.add_argument('--latency-multiplier', type=int, choices=[1,2,3], required=True)
    p.add_argument('--tick-size', type=float, required=True)
    p.add_argument('--lot-size', type=float, required=True)
    p.add_argument('--fee-rate', type=float, default=0.001)
    p.add_argument('--output', required=True)
    a=p.parse_args()

    plan=json.load(open(a.plan))
    if not isinstance(plan,list) or not plan:
        raise SystemExit('candidate order plan is empty')
    plan=sorted(plan,key=lambda x:int(x['entryTsNs']))
    for x in plan:
        if x.get('symbol') != a.symbol: raise SystemExit('plan contains wrong symbol')
        if str(x.get('side','BUY')).upper() != 'BUY': raise SystemExit('long-only runner rejects non-BUY entry')
        if int(x['exitTsNs']) <= int(x['entryTsNs']): raise SystemExit('exit must be after entry')

    latency=int(a.latency_ns*a.latency_multiplier)
    hbt=HashMapMarketDepthBacktest([mk_asset(a.data,a.tick_size,a.lot_size,a.queue,latency,a.fee_rate)])
    attempted=filled_entries=closed=0
    rets=[]; slip=[]; details=[]; order_id=1
    try:
        for x in plan:
            if not elapsed_to(hbt,int(x['entryTsNs'])): break
            attempted += 1
            intended=float(x['limitPrice']); qty=float(x['qty'])
            entry_id=order_id; order_id += 1
            rc=hbt.submit_buy_order(0,entry_id,intended,qty,GTC,LIMIT,True)
            if rc != 0: continue
            if not elapsed_to(hbt,int(x['exitTsNs'])): break
            eo=hbt.orders(0).get(entry_id)
            entry_qty=float(eo.exec_qty) if eo is not None and eo.status in ACTIVE_OR_FILLED else 0.0
            if entry_qty <= 0:
                if eo is not None and eo.cancellable: hbt.cancel(0,entry_id,True)
                details.append({'id':entry_id,'filled':False})
                continue
            filled_entries += 1
            entry_px=float(eo.exec_price)
            if eo.cancellable: hbt.cancel(0,entry_id,True)
            depth=hbt.depth(0)
            best_bid=float(depth.best_bid)
            if not math.isfinite(best_bid) or best_bid <= 0:
                details.append({'id':entry_id,'filled':True,'closed':False,'reason':'NO_BID'})
                continue
            exit_id=order_id; order_id += 1
            # IOC at best bid is deliberately marketable without inventing an off-book price.
            rc=hbt.submit_sell_order(0,exit_id,best_bid,entry_qty,IOC,LIMIT,True)
            xo=hbt.orders(0).get(exit_id)
            exit_qty=float(xo.exec_qty) if rc == 0 and xo is not None and xo.status in ACTIVE_OR_FILLED else 0.0
            if exit_qty <= 0:
                details.append({'id':entry_id,'filled':True,'closed':False,'reason':'EXIT_NOT_FILLED'})
                continue
            q=min(entry_qty,exit_qty); exit_px=float(xo.exec_price)
            gross=exit_px/entry_px-1.0
            net=gross-2*a.fee_rate
            rets.append(net); closed += 1
            slip.append((entry_px/intended-1.0)*10000.0)
            details.append({'id':entry_id,'filled':True,'closed':True,'entryPx':entry_px,'exitPx':exit_px,'qty':q,'netReturn':net})
    finally:
        hbt.close()

    fill_rate=filled_entries/attempted if attempted else 0.0
    expectancy=sum(rets)/len(rets) if rets else 0.0
    out={
      'symbol':a.symbol,'feed':a.feed,'queueModel':a.queue,'latencyMultiplier':a.latency_multiplier,
      'latencyNs':latency,'attempted':attempted,'filledEntries':filled_entries,'closedTrades':closed,
      'fillRate':fill_rate,'netExpectancy':expectancy,'profitFactor':profit_factor(rets),
      'slippageBps':sum(slip)/len(slip) if slip else 0.0,
      'authorization':'RESEARCH_ONLY','liveTrading':False,'details':details
    }
    Path(a.output).parent.mkdir(parents=True,exist_ok=True)
    json.dump(out,open(a.output,'w'),indent=2)
    print(json.dumps({k:v for k,v in out.items() if k!='details'},indent=2))

if __name__=='__main__': main()
