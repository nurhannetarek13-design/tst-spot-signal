from __future__ import annotations

"""Conservative recovery worker for Binance Spot execution.

It never opens a speculative BUY. It only:
1) resolves uncertain bot BUY/OCO submissions using deterministic client IDs;
2) restores local state from a confirmed Binance fill;
3) protects a confirmed bot BUY if protection was never submitted.

Non-bot orders are never cancelled or modified.
"""

import hashlib,hmac,json,os,time,urllib.error,urllib.parse,urllib.request

import binance_filters
import reconcile_state
import trade_state

POLL_SEC=max(10,int(os.getenv('EXECUTION_RECOVERY_POLL_SEC','15')))
RESOLVE_AFTER=max(45,int(os.getenv('EXECUTION_RESERVED_RESOLVE_AFTER_SEC','75')))
NOT_FOUND_AFTER=max(120,int(os.getenv('EXECUTION_UNKNOWN_NOT_FOUND_AFTER_SEC','180')))
AUTO_PROTECT=(os.getenv('AUTO_PROTECTION_RECOVERY','1').strip()=='1')
PORT=int(os.getenv('PORT','8080'))
TELEGRAM_BOT_TOKEN=(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
TELEGRAM_CHAT_ID=(os.getenv('TELEGRAM_CHAT_ID') or '').strip()


def _alert(text:str)->None:
    print(f'[execution-recovery] ALERT {text}',flush=True)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        data=urllib.parse.urlencode({'chat_id':TELEGRAM_CHAT_ID,'text':text,'disable_web_page_preview':'true'}).encode()
        req=urllib.request.Request(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',data=data,method='POST')
        with urllib.request.urlopen(req,timeout=10): pass
    except Exception as exc:
        print(f'[execution-recovery] alert warning {type(exc).__name__}: {str(exc)[:120]}',flush=True)


def _not_found(exc:Exception)->bool:
    if not isinstance(exc,urllib.error.HTTPError): return False
    try: body=exc.read().decode('utf-8','replace')
    except Exception: body=''
    return exc.code==400 and ('-2013' in body or '-2011' in body or 'does not exist' in body.lower())


def _body_from_reservation(row:dict)->dict:
    return {
        'signal_id':row.get('signal_id'),'symbol':row.get('symbol'),
        'client_order_id':row.get('client_order_id'),
        'list_client_order_id':row.get('list_client_order_id'),
        'stop_client_order_id':row.get('stop_client_order_id'),
        'limit_client_order_id':row.get('limit_client_order_id'),
        'take_profit_price':row.get('take_profit_price'),
        'model_take_profit_price':row.get('model_take_profit_price'),
        'stop_loss_price':row.get('stop_loss_price'),
        'stop_limit_price':row.get('stop_limit_price'),
        'quantity':row.get('quantity'),
    }


def _resolve_buy(row:dict)->None:
    signal_id=str(row.get('signal_id') or ''); symbol=str(row.get('symbol') or '')
    client_id=str(row.get('client_order_id') or '')
    if not signal_id or not symbol or not client_id: return
    age=time.time()-float(row.get('reserved_at') or 0)
    if age<RESOLVE_AFTER: return
    try:
        order=reconcile_state._signed_get('/api/v3/order',{'symbol':symbol,'origClientOrderId':client_id})
    except Exception as exc:
        if _not_found(exc) and age>=NOT_FOUND_AFTER:
            trade_state.update_reservation(signal_id,'BUY',status='NOT_FOUND',reconciled_at=time.time())
            trade_state.append_event('BUY_NOT_FOUND_AFTER_UNCERTAINTY',signal_id=signal_id,symbol=symbol,client_order_id=client_id)
        return
    status=str(order.get('status') or '').upper()
    qty=float(order.get('executedQty') or 0); quote=float(order.get('cummulativeQuoteQty') or 0)
    if qty>0 and quote>0:
        body=_body_from_reservation(row)
        resp={'ok':True,'status':'BUY_FILLED','signal_id':signal_id,'symbol':symbol,
              'order_id':order.get('orderId'),'client_order_id':client_id,
              'executed_qty':qty,'quote_spent':quote,'recovered':True}
        trade_state.record_buy(body,resp)
        trade_state.update_position(signal_id,status='PROTECTION_PENDING',recovered_from_uncertain_execution=True)
        trade_state.append_event('BUY_RECOVERED_BY_CLIENT_ID',signal_id=signal_id,symbol=symbol,order_id=order.get('orderId'),client_order_id=client_id,binance_status=status)
        print(f'[execution-recovery] BUY RECOVERED {symbol} signal={signal_id} qty={qty:.10g}',flush=True)
    elif status in {'REJECTED','EXPIRED','CANCELED'}:
        trade_state.update_reservation(signal_id,'BUY',status='REJECTED',binance_status=status,reconciled_at=time.time())


def _resolve_oco(row:dict)->None:
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


def _signed_local_oco(body:dict)->tuple[int,dict]:
    if not TELEGRAM_BOT_TOKEN: raise RuntimeError('TELEGRAM_BOT_TOKEN_MISSING')
    raw=json.dumps(body,separators=(',',':'),ensure_ascii=False).encode('utf-8')
    stamp=str(int(time.time()*1000))
    sig=hmac.new(TELEGRAM_BOT_TOKEN.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    req=urllib.request.Request(f'http://127.0.0.1:{PORT}/make-exec-relay',data=raw,method='POST',headers={
        'Content-Type':'application/json','X-Make-Relay-Timestamp':stamp,'X-Make-Relay-Signature':sig,'User-Agent':'tst-protection-recovery/1.0'})
    try:
        with urllib.request.urlopen(req,timeout=50) as r: return r.status,json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as exc:
        data=exc.read() or b'{}'
        try: row=json.loads(data)
        except Exception: row={'status':f'HTTP_{exc.code}'}
        return exc.code,row


def _recover_protection(pos:dict)->None:
    if not AUTO_PROTECT or str(pos.get('status') or '')!='PROTECTION_PENDING': return
    signal_id=str(pos.get('signal_id') or ''); symbol=str(pos.get('symbol') or '')
    if not signal_id or not symbol: return
    existing=trade_state.get_reservation(signal_id,'OCO')
    if existing:
        return  # resolver handles uncertain/placed OCO; never blind-resubmit
    try:
        qty=float(pos.get('quantity') or 0); tp=float(pos.get('target') or 0); sl=float(pos.get('stop') or 0)
    except Exception: qty=tp=sl=0
    if qty<=0 or tp<=sl or sl<=0:
        _alert(f'🚨 {symbol} BUY confirmed but protection intent is incomplete. New entries stay blocked.')
        return
    # Conservative stop-limit offset; Binance filter normalizer snaps it to tick size.
    sl_limit=sl*0.997
    body={'signal_id':signal_id,'action':'OCO','symbol':symbol,'quantity':qty,
          'take_profit_price':tp,'model_take_profit_price':pos.get('model_target'),
          'stop_loss_price':sl,'stop_limit_price':sl_limit,
          'confirmed':True,'dry_run':False,'timestamp':int(time.time())}
    try:
        code,row=_signed_local_oco(body)
        if code<300 and row.get('ok') is True:
            print(f'[execution-recovery] protection restored {symbol} signal={signal_id}',flush=True)
        else:
            _alert(f'🚨 Protection recovery failed for {symbol}: {row.get("status") or code}. New entries remain blocked.')
    except Exception as exc:
        _alert(f'🚨 Protection recovery transport failure for {symbol}: {type(exc).__name__}. New entries remain blocked.')


def run_once()->None:
    for row in trade_state.reservations('BUY',{'RESERVED','UNKNOWN'}): _resolve_buy(row)
    for row in trade_state.reservations('OCO',{'RESERVED','UNKNOWN'}): _resolve_oco(row)
    for pos in trade_state.open_positions(): _recover_protection(pos)
    snap=trade_state.portfolio_snapshot()
    print(f"[execution-recovery] OK open={snap['open_count']} incomplete={snap['incomplete_count']} risk={snap['stop_risk_usdt']:.4f}",flush=True)


def main()->None:
    print(f'[execution-recovery] ONLINE poll={POLL_SEC}s resolve_after={RESOLVE_AFTER}s auto_protect={AUTO_PROTECT}',flush=True)
    while True:
        try: run_once()
        except Exception as exc: print(f'[execution-recovery] loop warning {type(exc).__name__}: {str(exc)[:180]}',flush=True)
        time.sleep(POLL_SEC)


if __name__=='__main__': main()
