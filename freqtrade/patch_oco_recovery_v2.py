from pathlib import Path

p = Path('/freqtrade/execution_recovery.py')
s = p.read_text(encoding='utf-8')

# A Binance HTTP 400 was previously hidden by reconcile_state._signed_get(),
# leaving an OCO reservation UNKNOWN forever after Make returned a non-JSON
# error. Query orderList here with full Binance error-body visibility so a proven
# NOT_FOUND protection attempt can be released and safely retried.
helper_marker = "\n\ndef _resolve_oco(row:dict)->None:\n"
helper = r'''

def _query_order_list_by_client_id(client_id:str):
    k,secret=reconcile_state._creds()
    last=None
    for base in reconcile_state.API_BASES:
        params={'origClientOrderId':client_id,'timestamp':int(time.time()*1000),'recvWindow':5000}
        q=urllib.parse.urlencode(params)
        sig=hmac.new(secret.encode(),q.encode(),hashlib.sha256).hexdigest()
        req=urllib.request.Request(
            f'{base}/api/v3/orderList?{q}&signature={sig}',
            headers={'X-MBX-APIKEY':k,'User-Agent':'tst-protection-recovery/2.0'},
        )
        try:
            with urllib.request.urlopen(req,timeout=12) as resp:
                obj=json.loads(resp.read() or b'{}')
                return 'FOUND', obj if isinstance(obj,dict) else {}
        except urllib.error.HTTPError as exc:
            raw=exc.read() or b''
            try: err=json.loads(raw)
            except Exception: err={}
            code=err.get('code') if isinstance(err,dict) else None
            msg=str(err.get('msg') or '') if isinstance(err,dict) else ''
            if exc.code==400 and (code in {-2013,-2011} or 'does not exist' in msg.lower() or 'unknown order' in msg.lower()):
                return 'NOT_FOUND', {'code':code,'msg':msg}
            last=RuntimeError(f'HTTP_{exc.code}:{code}:{msg[:100]}')
        except Exception as exc:
            last=exc
    raise RuntimeError(f'ORDER_LIST_QUERY_FAILED:{type(last).__name__}:{str(last)[:140]}')
'''
if 'def _query_order_list_by_client_id(' not in s:
    if helper_marker not in s:
        raise SystemExit('oco-recovery-v2: helper marker missing')
    s=s.replace(helper_marker,helper+helper_marker,1)

old_resolve = '''def _resolve_oco(row:dict)->None:
    signal_id=str(row.get('signal_id') or ''); symbol=str(row.get('symbol') or '')
    client_id=str(row.get('list_client_order_id') or '')
    if not signal_id or not symbol or not client_id: return
    age=time.time()-float(row.get('reserved_at') or 0)
    if age<RESOLVE_AFTER: return
    try:
        oco=reconcile_state._signed_get('/api/v3/orderList',{'origClientOrderId':client_id})
    except Exception as exc:
        if _not_found(exc) and age>=NOT_FOUND_AFTER:
            trade_state.update_reservation(signal_id,'OCO',status='NOT_FOUND',reconciled_at=time.time())
            trade_state.append_event('OCO_NOT_FOUND_AFTER_UNCERTAINTY',signal_id=signal_id,symbol=symbol,list_client_order_id=client_id)
        return
    list_id=int(oco.get('orderListId') or 0)
    if list_id<=0: return
    body=_body_from_reservation(row)
    resp={'ok':True,'status':'OCO_PLACED','signal_id':signal_id,'symbol':symbol,'oco_order_list_id':list_id,'recovered':True}
    trade_state.record_oco(body,resp)
    trade_state.append_event('OCO_RECOVERED_BY_CLIENT_ID',signal_id=signal_id,symbol=symbol,order_list_id=list_id,list_client_order_id=client_id)
    print(f'[execution-recovery] OCO RECOVERED {symbol} signal={signal_id} list={list_id}',flush=True)
'''
new_resolve = '''def _resolve_oco(row:dict)->None:
    signal_id=str(row.get('signal_id') or ''); symbol=str(row.get('symbol') or '')
    client_id=str(row.get('list_client_order_id') or '')
    if not signal_id or not symbol or not client_id: return
    age=time.time()-float(row.get('reserved_at') or 0)
    if age<RESOLVE_AFTER: return
    try:
        state,oco=_query_order_list_by_client_id(client_id)
    except Exception as exc:
        print(f'[execution-recovery] OCO query warning {symbol} {type(exc).__name__}: {str(exc)[:120]}',flush=True)
        return
    if state=='NOT_FOUND':
        if age>=NOT_FOUND_AFTER:
            trade_state.update_reservation(signal_id,'OCO',status='NOT_FOUND',reconciled_at=time.time(),binance_not_found=True)
            trade_state.append_event('OCO_NOT_FOUND_AFTER_UNCERTAINTY',signal_id=signal_id,symbol=symbol,list_client_order_id=client_id)
            print(f'[execution-recovery] OCO NOT_FOUND {symbol} signal={signal_id}; terminal retry permitted',flush=True)
        return
    list_id=int(oco.get('orderListId') or 0)
    if list_id<=0: return
    body=_body_from_reservation(row)
    resp={'ok':True,'status':'OCO_PLACED','signal_id':signal_id,'symbol':symbol,'oco_order_list_id':list_id,'recovered':True}
    trade_state.record_oco(body,resp)
    trade_state.append_event('OCO_RECOVERED_BY_CLIENT_ID',signal_id=signal_id,symbol=symbol,order_list_id=list_id,list_client_order_id=client_id)
    print(f'[execution-recovery] OCO RECOVERED {symbol} signal={signal_id} list={list_id}',flush=True)
'''
if new_resolve not in s:
    if old_resolve not in s:
        raise SystemExit('oco-recovery-v2: resolve block marker missing')
    s=s.replace(old_resolve,new_resolve,1)

# Manual/external SELL is authoritative evidence that bot ownership changed.
# Detect it BEFORE an UNKNOWN reservation can early-return forever. We clear only
# bot state/reservation; we never place/cancel another order against that asset.
needle = '''    signal_id=str(pos.get('signal_id') or ''); symbol=str(pos.get('symbol') or '')
    if not signal_id or not symbol: return

    existing=trade_state.get_reservation(signal_id,'OCO')
'''
replacement = '''    signal_id=str(pos.get('signal_id') or ''); symbol=str(pos.get('symbol') or '')
    if not signal_id or not symbol: return

    try: tracked_qty=max(0.0,float(pos.get('quantity') or 0))
    except Exception: tracked_qty=0.0
    try: external_sell=reconcile_state.sell_qty_since(symbol,float(pos.get('opened_at') or 0))
    except Exception: external_sell=0.0
    tolerance=max(1e-12,tracked_qty*0.00025)
    if external_sell>tolerance:
        existing_now=trade_state.get_reservation(signal_id,'OCO')
        if existing_now:
            trade_state.update_reservation(signal_id,'OCO',status='ABANDONED_EXTERNAL_EXIT',reconciled_at=time.time(),sell_qty_since_open=external_sell)
        trade_state.mark_externally_closed(signal_id,reason='MANUAL_OR_EXTERNAL_EXIT_DETECTED',
                                           expected_bot_qty=tracked_qty,sell_qty_since_open=external_sell)
        _alert(f'ℹ️ {symbol} manual/external exit detected. Stale bot position cleared; no order was placed.')
        print(f'[execution-recovery] EXTERNAL_EXIT {symbol} signal={signal_id} expected={tracked_qty:.10g} sold_since={external_sell:.10g}',flush=True)
        return

    existing=trade_state.get_reservation(signal_id,'OCO')
'''
if replacement not in s:
    if needle not in s:
        raise SystemExit('oco-recovery-v2: manual exit insertion marker missing')
    s=s.replace(needle,replacement,1)

for marker in [
    'def _query_order_list_by_client_id(',
    "state=='NOT_FOUND'",
    'terminal retry permitted',
    "status='ABANDONED_EXTERNAL_EXIT'",
    '[execution-recovery] EXTERNAL_EXIT',
]:
    if marker not in s:
        raise SystemExit(f'oco-recovery-v2 invariant missing: {marker}')
compile(s,str(p),'exec')
p.write_text(s,encoding='utf-8')
print('[oco-recovery-v2] OK explicit Binance NOT_FOUND recovery + manual-exit cleanup before UNKNOWN lock')
