from pathlib import Path

path = Path('/freqtrade/dynamic_exit_manager.py')
s = path.read_text(encoding='utf-8')

# Prefer the already-authorized Make Binance connection for exact OCO cancellation.
# Railway's direct Binance key may be read-only / IP-restricted (-2015), while the
# Make connection is the production execution credential used for BUY/OCO.
start = s.find('def _write_permission() -> tuple[bool, str]:\n')
end = s.find('\ndef _market(symbol: str) -> dict:\n', start)
if start < 0 or end < 0:
    raise SystemExit('dynamic-exit-make-cancel: write-permission block missing')

new_permission = '''def _write_permission() -> tuple[bool, str]:
    now = time.time()
    if now - float(_write_permission_cache['checked_at']) < 600:
        return bool(_write_permission_cache['ok']), str(_write_permission_cache['reason'])
    if not LIVE_ENABLED:
        _write_permission_cache.update({'checked_at': now, 'ok': False, 'reason': 'live-disabled'})
        return False, 'live-disabled'
    # Production execution already uses the Make Binance connection.  When the
    # protected OCO webhook is configured it is our preferred exact-cancel route;
    # this avoids depending on Railway's separate write credential/IP policy.
    if MAKE_OCO_URL:
        _write_permission_cache.update({'checked_at': now, 'ok': True, 'reason': 'make-exact-oco-route'})
        return True, 'make-exact-oco-route'
    # Fallback only: direct Railway key. Probe with an impossible order id so no
    # real order can be changed.
    code, row = _signed_write('DELETE', '/api/v3/order', {'symbol': 'BTCUSDT', 'orderId': 922337203685477000})
    bcode = int(row.get('code') or 0) if isinstance(row, dict) else 0
    ok = bcode in {-2011, -2013}
    reason = 'direct-trade-write-authorized' if ok else f'write-probe-failed-http{code}-code{bcode}'
    _write_permission_cache.update({'checked_at': now, 'ok': ok, 'reason': reason})
    return ok, reason
'''
s = s[:start] + new_permission + s[end:]

insert_marker = '\ndef _record_new_oco(pos: dict, body: dict, row: dict, reason: str) -> None:\n'
if insert_marker not in s:
    raise SystemExit('dynamic-exit-make-cancel: record-new-oco marker missing')

helper = r'''

def _resolve_cancel_state(symbol: str, order_list_id: int) -> tuple[str, dict]:
    """Resolve an ambiguous exact-cancel without guessing.

    ACTIVE: original OCO is still protecting the position.
    CANCELLED_UNFILLED: list is no longer active and neither child filled, so it is
    safe to place the replacement OCO.
    FILLED_OR_PARTIAL: a child executed during the race; never place replacement.
    UNKNOWN: fail closed.
    """
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
        'signal_id': str(pos.get('signal_id') or ''),
        'action': 'CANCEL_OCO_EXACT',
        'symbol': symbol,
        'order_list_id': int(order_list_id),
        'confirmed': True,
        'dry_run': False,
        'timestamp': int(time.time()),
    }
    raw = json.dumps(body, separators=(',', ':')).encode()
    req = urllib.request.Request(
        MAKE_OCO_URL, data=raw, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'tst-dynamic-exit/1.1'},
    )
    uncertain = None
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            row = json.loads(r.read() or b'{}')
            if (r.status < 300 and row.get('ok') is True and
                    str(row.get('status') or '') == 'OCO_CANCELLED' and
                    int(row.get('oco_order_list_id') or 0) == int(order_list_id)):
                return True, row
            uncertain = row
    except urllib.error.HTTPError as exc:
        try:
            uncertain = json.loads(exc.read() or b'{}')
        except Exception:
            uncertain = {'status': f'HTTP_{exc.code}'}
    except Exception as exc:
        uncertain = {'status': f'{type(exc).__name__}:{str(exc)[:120]}'}

    # Webhook/transport failures are ambiguous. Resolve against Binance before
    # doing anything else so a successful cancel cannot leave the position naked.
    state, resolved = _resolve_cancel_state(symbol, int(order_list_id))
    if state == 'CANCELLED_UNFILLED':
        return True, {'ok': True, 'status': 'OCO_CANCELLED_RECONCILED',
                      'oco_order_list_id': int(order_list_id),
                      'transport_response': uncertain, 'resolved': resolved}
    if state == 'FILLED_OR_PARTIAL':
        return False, {'status': 'OCO_FILLED_OR_PARTIAL_DURING_CANCEL',
                       'order_list_id': int(order_list_id), 'resolved': resolved}
    if state == 'ACTIVE':
        return False, {'status': 'OCO_STILL_ACTIVE_AFTER_CANCEL_ATTEMPT',
                       'order_list_id': int(order_list_id), 'transport_response': uncertain}
    return False, {'status': 'OCO_CANCEL_STATE_UNKNOWN',
                   'order_list_id': int(order_list_id), 'transport_response': uncertain}


def _cancel_exact_oco(pos: dict, order_list_id: int) -> tuple[bool, dict]:
    if MAKE_OCO_URL:
        return _cancel_make_oco(pos, order_list_id)
    symbol = str(pos.get('symbol') or '')
    code, row = _signed_write('DELETE', '/api/v3/orderList', {'symbol': symbol, 'orderListId': int(order_list_id)})
    return bool(code < 300 and int(row.get('orderListId') or 0) == int(order_list_id)), row

'''
if 'def _cancel_make_oco(' not in s:
    s = s.replace(insert_marker, helper + insert_marker, 1)

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
        raise SystemExit('dynamic-exit-make-cancel: destructive cancel marker missing')
    s = s.replace(old_cancel, new_cancel, 1)

for marker in [
    "make-exact-oco-route",
    "def _cancel_make_oco(",
    "def _cancel_exact_oco(",
    "CANCEL_OCO_EXACT",
    "OCO_CANCELLED_RECONCILED",
    "cancelled, cancel = _cancel_exact_oco(pos, current_oid)",
]:
    if marker not in s:
        raise SystemExit(f'dynamic-exit-make-cancel: missing marker {marker}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[dynamic-exit-make-cancel-patch] OK exact bot-owned OCO cancellation routed through Make Binance connection; direct Railway -2015 no longer blocks live ratchet')
