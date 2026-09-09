from pathlib import Path

path = Path('/freqtrade/dynamic_exit_manager.py')
s = path.read_text(encoding='utf-8')

ids_marker = """def _ids(signal_id: str, seq: int) -> tuple[str, str, str]:
    d = hashlib.sha256(f'{signal_id}:{seq}'.encode()).hexdigest()[:22]
    return f'TSTO-R{d}', f'TSTS-R{d}', f'TSTL-R{d}'


"""
helper = ids_marker + r'''def _resolve_oco_client(symbol: str, list_client_order_id: str) -> dict | None:
    """Resolve a possibly successful OCO after a lost/ambiguous Make response."""
    if not symbol or not list_client_order_id:
        return None
    try:
        row = reconcile_state._signed_get('/api/v3/orderList', {'origClientOrderId': list_client_order_id})
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    if str(row.get('symbol') or '') != symbol:
        return None
    if str(row.get('listClientOrderId') or '') != list_client_order_id:
        return None
    oid = int(row.get('orderListId') or 0)
    if oid <= 0:
        return None
    return {
        'ok': True, 'status': 'OCO_PLACED', 'symbol': symbol,
        'oco_order_list_id': oid, 'resolved_after_uncertain_response': True,
    }


'''
if 'def _resolve_oco_client' not in s:
    if ids_marker not in s:
        raise SystemExit('dynamic-exit-v2: ids marker missing')
    s = s.replace(ids_marker, helper, 1)

old_live = """    placed, row, body = _place_make_oco(pos, quantity, target, suggested, seq)
    if placed:
        _record_new_oco(pos, body, row, reason)
"""
new_live = """    placed, row, body = _place_make_oco(pos, quantity, target, suggested, seq)
    if not placed and body.get('list_client_order_id'):
        resolved = _resolve_oco_client(symbol, str(body.get('list_client_order_id') or ''))
        if resolved:
            placed, row = True, resolved
            trade_state.append_event('OCO_RATCHET_RESPONSE_RECOVERED', signal_id=signal_id, symbol=symbol, list_client_order_id=body.get('list_client_order_id'), order_list_id=row.get('oco_order_list_id'))
    if placed:
        _record_new_oco(pos, body, row, reason)
"""
if 'OCO_RATCHET_RESPONSE_RECOVERED' not in s:
    if old_live not in s:
        raise SystemExit('dynamic-exit-v2: live placement marker missing')
    s = s.replace(old_live, new_live, 1)

old_rescue = """    rescue, rescue_row, rescue_body = _place_make_oco(pos, quantity, target, old_stop, seq + 100000)
    if rescue:
        _record_new_oco(pos, rescue_body, rescue_row, 'ratchet-rollback')
"""
new_rescue = """    rescue, rescue_row, rescue_body = _place_make_oco(pos, quantity, target, old_stop, seq + 100000)
    if not rescue and rescue_body.get('list_client_order_id'):
        resolved = _resolve_oco_client(symbol, str(rescue_body.get('list_client_order_id') or ''))
        if resolved:
            rescue, rescue_row = True, resolved
            trade_state.append_event('OCO_RATCHET_ROLLBACK_RESPONSE_RECOVERED', signal_id=signal_id, symbol=symbol, list_client_order_id=rescue_body.get('list_client_order_id'), order_list_id=rescue_row.get('oco_order_list_id'))
    if rescue:
        _record_new_oco(pos, rescue_body, rescue_row, 'ratchet-rollback')
"""
if 'OCO_RATCHET_ROLLBACK_RESPONSE_RECOVERED' not in s:
    if old_rescue not in s:
        raise SystemExit('dynamic-exit-v2: rollback marker missing')
    s = s.replace(old_rescue, new_rescue, 1)

# Prefer the production Make Binance connection for exact OCO cancellation.
# Railway's direct Binance credential may be read-only/IP-restricted (-2015).
perm_start = s.find('def _write_permission() -> tuple[bool, str]:\n')
perm_end = s.find('\ndef _market(symbol: str) -> dict:\n', perm_start)
if perm_start < 0 or perm_end < 0:
    raise SystemExit('dynamic-exit-v2: write-permission block missing')
new_permission = '''def _write_permission() -> tuple[bool, str]:
    now = time.time()
    if now - float(_write_permission_cache['checked_at']) < 600:
        return bool(_write_permission_cache['ok']), str(_write_permission_cache['reason'])
    if not LIVE_ENABLED:
        _write_permission_cache.update({'checked_at': now, 'ok': False, 'reason': 'live-disabled'})
        return False, 'live-disabled'
    if MAKE_OCO_URL:
        _write_permission_cache.update({'checked_at': now, 'ok': True, 'reason': 'make-exact-oco-route'})
        return True, 'make-exact-oco-route'
    code, row = _signed_write('DELETE', '/api/v3/order', {'symbol': 'BTCUSDT', 'orderId': 922337203685477000})
    bcode = int(row.get('code') or 0) if isinstance(row, dict) else 0
    ok = bcode in {-2011, -2013}
    reason = 'direct-trade-write-authorized' if ok else f'write-probe-failed-http{code}-code{bcode}'
    _write_permission_cache.update({'checked_at': now, 'ok': ok, 'reason': reason})
    return ok, reason
'''
s = s[:perm_start] + new_permission + s[perm_end:]

record_marker = '\ndef _record_new_oco(pos: dict, body: dict, row: dict, reason: str) -> None:\n'
if record_marker not in s:
    raise SystemExit('dynamic-exit-v2: record-new-oco marker missing')
make_cancel_helper = r'''

def _resolve_cancel_state(symbol: str, order_list_id: int) -> tuple[str, dict]:
    try:
        row = reconcile_state._signed_get('/api/v3/orderList', {'orderListId': int(order_list_id)})
        if not isinstance(row, dict) or str(row.get('symbol') or '') != symbol:
            return 'UNKNOWN', row if isinstance(row, dict) else {}
        details = []
        for x in row.get('orders') or []:
            try:
                details.append(reconcile_state._order(symbol, int(x.get('orderId') or 0)))
            except Exception:
                pass
        if any(float(x.get('executedQty') or 0.0) > 0 for x in details):
            return 'FILLED_OR_PARTIAL', row
        status = str(row.get('listOrderStatus') or '').upper()
        if status in {'EXECUTING', 'NEW'}:
            return 'ACTIVE', row
        return 'CANCELLED_UNFILLED', row
    except Exception as exc:
        return 'UNKNOWN', {'status': f'{type(exc).__name__}:{str(exc)[:120]}'}


def _cancel_make_oco(pos: dict, order_list_id: int) -> tuple[bool, dict]:
    if not MAKE_OCO_URL:
        return False, {'status': 'MAKE_OCO_URL_MISSING'}
    symbol = str(pos.get('symbol') or '')
    body = {
        'signal_id': str(pos.get('signal_id') or ''), 'action': 'CANCEL_OCO_EXACT',
        'symbol': symbol, 'order_list_id': int(order_list_id),
        'confirmed': True, 'dry_run': False, 'timestamp': int(time.time()),
    }
    raw = json.dumps(body, separators=(',', ':')).encode()
    req = urllib.request.Request(MAKE_OCO_URL, data=raw, method='POST', headers={'Content-Type': 'application/json', 'User-Agent': 'tst-dynamic-exit/1.1'})
    uncertain = None
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            row = json.loads(r.read() or b'{}')
            if (r.status < 300 and row.get('ok') is True and str(row.get('status') or '') == 'OCO_CANCELLED' and int(row.get('oco_order_list_id') or 0) == int(order_list_id)):
                return True, row
            uncertain = row
    except urllib.error.HTTPError as exc:
        try:
            uncertain = json.loads(exc.read() or b'{}')
        except Exception:
            uncertain = {'status': f'HTTP_{exc.code}'}
    except Exception as exc:
        uncertain = {'status': f'{type(exc).__name__}:{str(exc)[:120]}'}
    state, resolved = _resolve_cancel_state(symbol, int(order_list_id))
    if state == 'CANCELLED_UNFILLED':
        return True, {'ok': True, 'status': 'OCO_CANCELLED_RECONCILED', 'oco_order_list_id': int(order_list_id), 'transport_response': uncertain, 'resolved': resolved}
    if state == 'FILLED_OR_PARTIAL':
        return False, {'status': 'OCO_FILLED_OR_PARTIAL_DURING_CANCEL', 'order_list_id': int(order_list_id), 'resolved': resolved}
    if state == 'ACTIVE':
        return False, {'status': 'OCO_STILL_ACTIVE_AFTER_CANCEL_ATTEMPT', 'order_list_id': int(order_list_id), 'transport_response': uncertain}
    return False, {'status': 'OCO_CANCEL_STATE_UNKNOWN', 'order_list_id': int(order_list_id), 'transport_response': uncertain}


def _cancel_exact_oco(pos: dict, order_list_id: int) -> tuple[bool, dict]:
    if MAKE_OCO_URL:
        return _cancel_make_oco(pos, order_list_id)
    symbol = str(pos.get('symbol') or '')
    code, row = _signed_write('DELETE', '/api/v3/orderList', {'symbol': symbol, 'orderListId': int(order_list_id)})
    return bool(code < 300 and int(row.get('orderListId') or 0) == int(order_list_id)), row

'''
if 'def _cancel_make_oco(' not in s:
    s = s.replace(record_marker, make_cancel_helper + record_marker, 1)

old_cancel = """    code, cancel = _signed_write('DELETE', '/api/v3/orderList', {'symbol': symbol, 'orderListId': current_oid})
    if code >= 300 or int(cancel.get('orderListId') or 0) != current_oid:
        trade_state.append_event('OCO_RATCHET_CANCEL_FAILED', signal_id=signal_id, symbol=symbol, order_list_id=current_oid, response=cancel)
        return
"""
new_cancel = """    cancelled, cancel = _cancel_exact_oco(pos, current_oid)
    if not cancelled:
        trade_state.append_event('OCO_RATCHET_CANCEL_FAILED', signal_id=signal_id, symbol=symbol, order_list_id=current_oid, response=cancel)
        return
"""
if new_cancel not in s:
    if old_cancel not in s:
        raise SystemExit('dynamic-exit-v2: destructive cancel marker missing')
    s = s.replace(old_cancel, new_cancel, 1)

required = [
    'def _resolve_oco_client', 'OCO_RATCHET_RESPONSE_RECOVERED',
    'OCO_RATCHET_ROLLBACK_RESPONSE_RECOVERED', 'make-exact-oco-route',
    'def _cancel_make_oco(', 'CANCEL_OCO_EXACT',
    'cancelled, cancel = _cancel_exact_oco(pos, current_oid)',
]
for item in required:
    if item not in s:
        raise SystemExit(f'dynamic-exit-v2: missing {item}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[dynamic-exit-v2-patch] OK Make-backed exact OCO cancel + ambiguous OCO recovery + fail-closed replacement')
