from __future__ import annotations

"""Read-only Binance reconciliation for canonical live trade state.

The reconciler never places/cancels an order. It rebuilds bot-owned OCOs after a
restart and records an actual exit only when a Binance SELL child order has
executed quantity. A disappeared/cancelled OCO with no exit is NOT treated as a
closed position; it becomes PROTECTION_PENDING so fresh entries fail closed.
"""

import hashlib,hmac,json,os,sys,time,urllib.error,urllib.parse,urllib.request
from collections import defaultdict
import trade_state

API_BASES=['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com']
POLL_SEC=max(30,int(os.getenv('RECONCILE_POLL_SEC','60')))
# Conservative buffer because Binance commission may be paid in base/quote/BNB
# and cummulativeQuoteQty does not always contain the full fee economics.
PNL_COST_BUFFER_PCT=max(0.0,float(os.getenv('REALIZED_PNL_COST_BUFFER_PCT','0.0020')))


def _creds():
    k=(os.getenv('BINANCE_READ_API_KEY') or '').strip(); s=(os.getenv('BINANCE_READ_API_SECRET') or '').strip()
    if not k or not s: raise RuntimeError('READ_ONLY_BINANCE_CREDENTIALS_MISSING')
    return k,s


def _signed_get(path,params):
    k,s=_creds(); p=dict(params); p['timestamp']=int(time.time()*1000); p['recvWindow']=5000
    q=urllib.parse.urlencode(p); sig=hmac.new(s.encode(),q.encode(),hashlib.sha256).hexdigest(); last=None
    for base in API_BASES:
        try:
            req=urllib.request.Request(f'{base}{path}?{q}&signature={sig}',headers={'X-MBX-APIKEY':k,'User-Agent':'tst-reconciler/1.2'})
            with urllib.request.urlopen(req,timeout=12) as r: return json.loads(r.read() or b'{}')
        except Exception as exc: last=exc
    raise RuntimeError(f'BINANCE_RECONCILE_READ_FAILED:{type(last).__name__}:{str(last)[:120]}')


def _order(symbol,order_id):
    row=_signed_get('/api/v3/order',{'symbol':symbol,'orderId':int(order_id)}); return row if isinstance(row,dict) else {}


def _entry_from_recent_trades(symbol,around_ms,protected_qty):
    rows=_signed_get('/api/v3/myTrades',{'symbol':symbol,'limit':100}); groups=defaultdict(list)
    for row in rows if isinstance(rows,list) else []:
        try:
            if not bool(row.get('isBuyer')): continue
            t=int(row.get('time') or 0)
            if t>around_ms+120000 or t<around_ms-6*3600000: continue
            groups[int(row.get('orderId'))].append(row)
        except Exception: continue
    candidates=[]
    for oid,items in groups.items():
        qty=sum(float(x.get('qty') or 0) for x in items); quote=sum(float(x.get('qty') or 0)*float(x.get('price') or 0) for x in items); last_t=max(int(x.get('time') or 0) for x in items)
        if qty>0 and quote>0 and qty+1e-12>=protected_qty*.95: candidates.append((last_t,quote/qty,quote,oid))
    if not candidates: return 0.0,0.0,None
    candidates.sort(reverse=True); _,entry,quote,oid=candidates[0]; return entry,quote,oid


def _open_bot_ocos():
    rows=_signed_get('/api/v3/openOrderList',{}); return [r for r in rows if isinstance(r,dict) and str(r.get('listClientOrderId') or '').startswith('TSTO-')] if isinstance(rows,list) else []


def _rebuild_unknown_open_oco(row):
    try:
        symbol=str(row.get('symbol') or '').upper(); list_id=int(row.get('orderListId') or 0); trans_ms=int(row.get('transactionTime') or 0); orders=row.get('orders') or []
        if not symbol or list_id<=0 or len(orders)<2: return False
        details=[_order(symbol,int(x.get('orderId') or 0)) for x in orders if int(x.get('orderId') or 0)>0]
        tp_order=next((x for x in details if str(x.get('type') or '').upper() in {'LIMIT_MAKER','LIMIT'}),None); stop_order=next((x for x in details if str(x.get('type') or '').upper() in {'STOP_LOSS_LIMIT','STOP_LOSS'}),None)
        if not tp_order or not stop_order: return False
        qty=min(float(tp_order.get('origQty') or 0),float(stop_order.get('origQty') or 0)); tp=float(tp_order.get('price') or 0); stop=float(stop_order.get('stopPrice') or 0); stop_limit=float(stop_order.get('price') or stop)
        if qty<=0 or tp<=0 or stop<=0: return False
        entry,quote,buy_order_id=_entry_from_recent_trades(symbol,trans_ms or int(time.time()*1000),qty); signal_id=f'REC-{symbol}-{list_id}'
        state=trade_state.load_state()
        if signal_id in state.get('positions',{}): return True
        state['positions'][signal_id]={'signal_id':signal_id,'symbol':symbol,'entry':entry,'quantity':qty,'quote_spent':quote,'buy_order_id':buy_order_id,'target':tp,'stop':stop,'stop_limit':stop_limit,'oco_order_list_id':list_id,'oco_list_client_order_id':row.get('listClientOrderId'),'status':'OCO_ACTIVE','opened_at':(trans_ms/1000.0) if trans_ms else time.time(),'recovered_from_binance':True,'updated_at':time.time()}
        trade_state.save_state(state); trade_state.append_event('POSITION_RECOVERED',signal_id=signal_id,symbol=symbol,order_list_id=list_id,entry=entry,quantity=qty,target=tp,stop=stop)
        print(f'[reconcile] RECOVERED {symbol} list={list_id} entry={entry:.10g} qty={qty:.10g}',flush=True); return True
    except Exception as exc:
        print(f'[reconcile] rebuild warning {type(exc).__name__}: {str(exc)[:180]}',flush=True); return False


def _exit_from_finished_list(symbol:str,row:dict,pos:dict):
    """Return the executed SELL child, if any; otherwise None."""
    orders=row.get('orders') or []
    details=[]
    for x in orders:
        try:
            oid=int(x.get('orderId') or 0)
            if oid>0: details.append(_order(symbol,oid))
        except Exception: pass
    fills=[]
    for o in details:
        try:
            if str(o.get('side') or '').upper()!='SELL': continue
            qty=float(o.get('executedQty') or 0); quote=float(o.get('cummulativeQuoteQty') or 0)
            if qty>0 and quote>0: fills.append((qty,quote,o))
        except Exception: continue
    if not fills: return None
    # OCO should have one executed leg. If an abnormal partial exists on both,
    # use the one with the greater executed quote and log exact Binance details.
    qty,quote,o=max(fills,key=lambda z:z[1])
    exit_price=quote/qty
    typ=str(o.get('type') or '').upper()
    reason='TAKE_PROFIT' if typ in {'LIMIT_MAKER','LIMIT'} else ('STOP_LOSS' if 'STOP' in typ else 'BINANCE_OCO_EXIT')
    entry=float(pos.get('entry') or 0)
    cost_basis=entry*qty if entry>0 else 0.0
    conservative_cost=cost_basis*PNL_COST_BUFFER_PCT
    pnl=quote-cost_basis-conservative_cost if cost_basis>0 else 0.0
    closed_ms=int(o.get('updateTime') or o.get('time') or 0)
    return {
        'exit_price':exit_price,'exit_qty':qty,'exit_quote':quote,'realized_pnl_usdt':pnl,
        'close_reason':reason,'exit_order_id':int(o.get('orderId') or 0),
        'exit_order_type':typ,'exit_binance_status':o.get('status'),
        'pnl_cost_buffer_pct':PNL_COST_BUFFER_PCT,
        'closed_at':closed_ms/1000.0 if closed_ms>0 else time.time(),
    }


def run_once():
    open_ocos=_open_bot_ocos(); open_ids={int(x.get('orderListId') or 0) for x in open_ocos}; state=trade_state.load_state()
    known_by_list={int(v.get('oco_order_list_id') or 0):k for k,v in (state.get('positions') or {}).items() if isinstance(v,dict) and int(v.get('oco_order_list_id') or 0)>0}
    unresolved=[]
    for row in open_ocos:
        list_id=int(row.get('orderListId') or 0)
        if list_id not in known_by_list and not _rebuild_unknown_open_oco(row): unresolved.append(list_id)
    if unresolved: raise RuntimeError(f'UNRESOLVED_BOT_OCO:{unresolved}')

    state=trade_state.load_state()
    for signal_id,pos in list((state.get('positions') or {}).items()):
        if not isinstance(pos,dict) or pos.get('status')!='OCO_ACTIVE': continue
        oid=int(pos.get('oco_order_list_id') or 0)
        if oid<=0 or oid in open_ids: continue
        row=_signed_get('/api/v3/orderList',{'orderListId':oid})
        status=str(row.get('listOrderStatus') or '').upper(); status_type=str(row.get('listStatusType') or '').upper()
        finished=status in {'ALL_DONE','REJECT'} or status_type in {'ALL_DONE','RESPONSE'}
        if not finished: continue
        exit_row=_exit_from_finished_list(str(pos.get('symbol') or ''),row,pos)
        if exit_row:
            trade_state.close_position(signal_id,**exit_row,order_list_id=oid)
            print(f"[reconcile] CLOSED {pos.get('symbol')} reason={exit_row['close_reason']} pnl≈{exit_row['realized_pnl_usdt']:+.4f}USDT list={oid}",flush=True)
        else:
            # Critical correctness fix: a cancelled/rejected OCO without an exit
            # does not mean the coin position is closed.
            trade_state.update_position(signal_id,status='PROTECTION_PENDING',protection_lost_at=time.time(),close_reason=None)
            trade_state.update_reservation(signal_id,'OCO',status='CLOSED_NO_FILL',reconciled_at=time.time(),binance_list_status=status)
            trade_state.append_event('PROTECTION_LOST_NO_EXIT',signal_id=signal_id,symbol=pos.get('symbol'),order_list_id=oid,binance_list_status=status)
            print(f"[reconcile] CRITICAL {pos.get('symbol')} OCO ended without SELL fill; position remains open/unprotected and fresh entries are blocked",flush=True)

    snap=trade_state.portfolio_snapshot(); perf=trade_state.performance_snapshot()
    print(f"[reconcile] OK bot_open_ocos={len(open_ocos)} tracked_open={snap['open_count']} risk={snap['stop_risk_usdt']:.4f} incomplete={snap['incomplete_count']} pnl_today={perf['realized_pnl_today_usdt']:+.4f} streak={perf['consecutive_losses']}",flush=True)


def main():
    print(f'[reconcile] ONLINE read_only=True poll={POLL_SEC}s ownership_prefix=TSTO- fail_closed=True pnl_reconciliation=ON cost_buffer={PNL_COST_BUFFER_PCT:.3%}',flush=True)
    while True:
        try: run_once()
        except Exception as exc: print(f'[reconcile] loop warning {type(exc).__name__}: {str(exc)[:180]}',flush=True)
        time.sleep(POLL_SEC)

if __name__=='__main__':
    if '--once' in sys.argv: run_once()
    else: main()
