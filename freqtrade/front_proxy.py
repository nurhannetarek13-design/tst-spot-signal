from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import binance_filters
import trade_state

PORT=int(os.getenv('PORT','8080'))
BRIDGE_PORT=int(os.getenv('BRIDGE_PORT','8082'))
MAKE_BUY_WEBHOOK_URL=(os.getenv('MAKE_ONE_TAP_WEBHOOK_URL') or '').strip()
MAKE_OCO_WEBHOOK_URL=(os.getenv('MAKE_ONE_TAP_OCO_WEBHOOK_URL') or '').strip()
TELEGRAM_BOT_TOKEN=(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
try:
    MAX_EXECUTION_STAKE_USDT=max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','40')))
except Exception:
    MAX_EXECUTION_STAKE_USDT=40.0
try:
    MAX_SIGNAL_AGE_SEC=max(15,int(os.getenv('MAX_SIGNAL_AGE_SEC','120')))
except Exception:
    MAX_SIGNAL_AGE_SEC=120


def _json_bytes(payload):
    return json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode('utf-8')


def _signal_epoch(body):
    try:
        ts=float(body.get('timestamp') or 0)
        if ts > 10_000_000_000:
            ts/=1000.0
        return ts
    except Exception:
        return 0.0


class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def send_json(self,status,payload):
        data=_json_bytes(payload)
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Connection','close')
        self.end_headers()
        self.wfile.write(data)

    def make_exec_relay(self):
        if self.command!='POST':
            return self.send_json(405,{'ok':False,'status':'METHOD_NOT_ALLOWED'})
        if not TELEGRAM_BOT_TOKEN:
            return self.send_json(503,{'ok':False,'status':'MAKE_RELAY_NOT_CONFIGURED'})
        length=int(self.headers.get('Content-Length') or 0)
        if length<=0 or length>8192:
            return self.send_json(400,{'ok':False,'status':'BAD_BODY'})
        raw=self.rfile.read(length)
        ts=str(self.headers.get('X-Make-Relay-Timestamp') or '')
        supplied=str(self.headers.get('X-Make-Relay-Signature') or '').lower()
        try:
            stamp=int(ts)
        except Exception:
            return self.send_json(401,{'ok':False,'status':'BAD_TIMESTAMP'})
        if abs(int(time.time()*1000)-stamp)>60000:
            return self.send_json(401,{'ok':False,'status':'STALE_RELAY'})
        expected=hmac.new(TELEGRAM_BOT_TOKEN.encode('utf-8'),ts.encode('utf-8')+b'.'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied,expected):
            return self.send_json(401,{'ok':False,'status':'BAD_RELAY_SIGNATURE'})
        try:
            body=json.loads(raw or b'{}')
        except Exception:
            return self.send_json(400,{'ok':False,'status':'BAD_JSON'})
        signal_id=str(body.get('signal_id') or body.get('id') or '').strip()
        symbol=str(body.get('symbol') or '').upper()
        action=str(body.get('action') or '').upper()
        if not signal_id:
            return self.send_json(400,{'ok':False,'status':'MISSING_SIGNAL_ID'})
        if body.get('confirmed') is not True or body.get('dry_run') is not False:
            return self.send_json(409,{'ok':False,'status':'CONFIRMATION_REQUIRED'})
        if not re.fullmatch(r'[A-Z0-9]{1,20}USDT',symbol):
            return self.send_json(400,{'ok':False,'status':'BAD_SYMBOL'})
        signal_ts=_signal_epoch(body)
        age=time.time()-signal_ts if signal_ts>0 else 1e9
        if age < -15 or age > MAX_SIGNAL_AGE_SEC:
            return self.send_json(409,{'ok':False,'status':'STALE_SIGNAL','ageSeconds':round(age,1),'maxAgeSeconds':MAX_SIGNAL_AGE_SEC})

        try:
            if action=='BUY':
                try: quote=float(body.get('quote_amount_usdt') or 0)
                except Exception: quote=0
                if not (5<=quote<=MAX_EXECUTION_STAKE_USDT):
                    return self.send_json(400,{'ok':False,'status':'BAD_STAKE','maxStakeUSDT':MAX_EXECUTION_STAKE_USDT})
                quote,filter_meta=binance_filters.normalize_buy_quote(symbol,quote)
                body['quote_amount_usdt']=quote
                body['execution_filter_meta']=filter_meta
                target_url=MAKE_BUY_WEBHOOK_URL
            elif action=='OCO':
                try:
                    quantity=float(body.get('quantity') or 0)
                    tp=float(body.get('take_profit_price') or 0)
                    sl=float(body.get('stop_loss_price') or 0)
                    sl_limit=float(body.get('stop_limit_price') or 0)
                except Exception:
                    quantity=tp=sl=sl_limit=0
                if not (quantity>0 and tp>0 and sl>0 and sl_limit>0 and sl_limit<=sl<tp):
                    return self.send_json(400,{'ok':False,'status':'BAD_OCO_LEVELS'})
                pos=trade_state.position_for_signal(signal_id)
                if not pos or str(pos.get('status') or '') not in {'BUY_FILLED','PROTECTION_PENDING','OCO_ACTIVE'}:
                    return self.send_json(409,{'ok':False,'status':'UNKNOWN_BUY_FOR_OCO','signal_id':signal_id})
                normalized,filter_meta=binance_filters.normalize_oco(symbol,quantity,tp,sl,sl_limit)
                body.update(normalized)
                body['execution_filter_meta']=filter_meta
                target_url=MAKE_OCO_WEBHOOK_URL
            else:
                return self.send_json(400,{'ok':False,'status':'BAD_ACTION'})
        except Exception as exc:
            trade_state.append_event('FILTER_NORMALIZATION_BLOCK',signal_id=signal_id,action=action,symbol=symbol,error=str(exc)[:180])
            return self.send_json(409,{'ok':False,'status':'BINANCE_FILTER_BLOCK','action':action,'signal_id':signal_id,'reason':str(exc)[:180]})

        if not target_url:
            return self.send_json(503,{'ok':False,'status':'MAKE_ROUTE_NOT_CONFIGURED'})

        reserved,current=trade_state.reserve_execution(body,action)
        if not reserved:
            previous_response=current.get('response') if isinstance(current,dict) else None
            previous_status=str(current.get('status') or '') if isinstance(current,dict) else ''
            if isinstance(previous_response,dict) and previous_status in {'FILLED','PLACED'}:
                duplicate=dict(previous_response)
                duplicate['idempotentReplay']=True
                return self.send_json(200,duplicate)
            return self.send_json(409,{
                'ok':False,
                'status':'DUPLICATE_OR_UNCERTAIN_EXECUTION_BLOCKED',
                'signal_id':signal_id,
                'action':action,
                'reservationStatus':previous_status or 'UNKNOWN',
            })

        make_raw=_json_bytes(body)
        req=urllib.request.Request(target_url,data=make_raw,method='POST',headers={'Content-Type':'application/json','Cache-Control':'no-store','User-Agent':'tst-make-relay/5.1'})
        try:
            with urllib.request.urlopen(req,timeout=45) as r:
                data=r.read(); status=r.status
        except urllib.error.HTTPError as exc:
            status=exc.code; data=exc.read() or b'{}'
        except Exception as exc:
            trade_state.update_reservation(signal_id,action,status='UNKNOWN',error=f'{type(exc).__name__}:{str(exc)[:160]}')
            trade_state.append_event('EXECUTION_UNKNOWN',signal_id=signal_id,action=action,symbol=symbol,error=type(exc).__name__)
            print(f'[make-relay] {action} transport unknown-state: {type(exc).__name__}: {str(exc)[:160]}',flush=True)
            return self.send_json(502,{'ok':False,'status':'EXECUTION_STATE_UNKNOWN_NO_RETRY','action':action,'signal_id':signal_id})

        try:
            row=json.loads(data or b'{}')
        except Exception:
            trade_state.update_reservation(signal_id,action,status='UNKNOWN',error=f'NON_JSON_HTTP_{status}')
            print(f'[make-relay] {action} upstream non-json status={status} bytes={len(data)}',flush=True)
            code=status if status>=400 else 502
            label=f'MAKE_HTTP_{status}' if status>=400 else 'MAKE_NON_JSON_RESPONSE'
            return self.send_json(code,{'ok':False,'status':label,'action':action,'signal_id':signal_id})
        if not isinstance(row,dict):
            trade_state.update_reservation(signal_id,action,status='UNKNOWN',error='NON_OBJECT_JSON')
            print(f'[make-relay] {action} upstream non-object-json status={status}',flush=True)
            return self.send_json(502,{'ok':False,'status':'MAKE_BAD_JSON_RESPONSE','action':action,'signal_id':signal_id})

        try:
            if status < 400 and row.get('ok') is True:
                if action == 'BUY' and str(row.get('status') or '') == 'BUY_FILLED':
                    trade_state.record_buy(body, row)
                    trade_state.update_position(signal_id,status='PROTECTION_PENDING')
                    print(f"[trade-state] BUY tracked signal={row.get('signal_id') or signal_id} symbol={symbol} protection=PENDING", flush=True)
                elif action == 'OCO' and str(row.get('status') or '') == 'OCO_PLACED':
                    trade_state.record_oco(body, row)
                    print(f"[trade-state] OCO tracked signal={row.get('signal_id') or signal_id} symbol={symbol} list={row.get('oco_order_list_id')}", flush=True)
                else:
                    trade_state.update_reservation(signal_id,action,status='UNKNOWN',response=row,error='SUCCESS_WITH_UNEXPECTED_STATUS')
            else:
                trade_state.update_reservation(signal_id,action,status='REJECTED',response=row,http_status=status)
        except Exception as exc:
            print(f'[trade-state] tracking warning {type(exc).__name__}: {exc}', flush=True)

        return self.send_json(status,row)

    def proxy(self):
        if self.path=='/health' or self.path.startswith('/health?'):
            snap=trade_state.portfolio_snapshot()
            return self.send_json(200,{'ok':True,'status':'HEALTHY','role':'SIGNED_MAKE_RELAY','telegramOwner':'CLOUDFLARE','legacyExecution':False,'makeBuyConfigured':bool(MAKE_BUY_WEBHOOK_URL),'makeOcoConfigured':bool(MAKE_OCO_WEBHOOK_URL),'maxExecutionStakeUSDT':MAX_EXECUTION_STAKE_USDT,'maxSignalAgeSec':MAX_SIGNAL_AGE_SEC,'persistentState':str(trade_state.STATE_PATH).startswith('/data/'),'filterNormalizer':True,'idempotency':True,'trackedOpenPositions':snap.get('open_count',0),'trackedStopRiskUSDT':round(float(snap.get('stop_risk_usdt',0)),4),'incompleteTrackedPositions':snap.get('incomplete_count',0)})
        if self.path=='/signer/validate' or self.path.startswith('/signer/validate?'):
            return self.send_json(200,{'ok':True,'status':'SIGNER_VALIDATION_COMPAT','legacyExecution':False,'executionRoute':'TELEGRAM_CONFIRM_CLOUDFLARE_MAKE_ONLY','makeBuyConfigured':bool(MAKE_BUY_WEBHOOK_URL),'makeOcoConfigured':bool(MAKE_OCO_WEBHOOK_URL),'maxExecutionStakeUSDT':MAX_EXECUTION_STAKE_USDT})
        if self.path.startswith('/make-exec-relay'):
            return self.make_exec_relay()
        if self.path.startswith('/execute') or self.path.startswith('/signer/'):
            return self.send_json(410,{'ok':False,'status':'LEGACY_DIRECT_EXECUTION_DISABLED','executionRoute':'TELEGRAM_CONFIRM_CLOUDFLARE_MAKE_ONLY'})
        return self.send_json(404,{'ok':False,'status':'NOT_FOUND'})

    do_GET=proxy
    do_POST=proxy
    def log_message(self,*_): pass

if __name__=='__main__':
    print(f'[front-proxy] ONLINE role=signed-make-relay make_buy={bool(MAKE_BUY_WEBHOOK_URL)} make_oco={bool(MAKE_OCO_WEBHOOK_URL)} max_stake={MAX_EXECUTION_STAKE_USDT:.2f} trade_state=PERSISTENT idempotency=ON filter_normalizer=ON', flush=True)
    ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
