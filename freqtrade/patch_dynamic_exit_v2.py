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

# Restore the old Telegram lifecycle idea without creating another execution
# owner: 45m review + 60m decision deadline are notifications only. The OCO and
# dynamic profit-lock remain the automated protection layer.
review_anchor = "_write_permission_cache = {'checked_at': 0.0, 'ok': False, 'reason': 'not-checked'}\n"
review_consts = review_anchor + (
    "POSITION_REVIEW_45_SEC = max(300, int(os.getenv('POSITION_REVIEW_45_SEC', str(45 * 60))))\n"
    "POSITION_REVIEW_60_SEC = max(POSITION_REVIEW_45_SEC + 60, int(os.getenv('POSITION_REVIEW_60_SEC', str(60 * 60))))\n"
)
if 'POSITION_REVIEW_45_SEC =' not in s:
    if review_anchor not in s:
        raise SystemExit('dynamic-exit-v2: review constants anchor missing')
    s = s.replace(review_anchor, review_consts, 1)

run_marker = '\ndef run_once() -> None:\n'
review_helper = r'''

def _position_status_label(pnl_pct: float, peak_pct: float) -> str:
    if pnl_pct >= 0.50:
        return 'قوية 🟢'
    if pnl_pct >= -0.20:
        return 'شبه ثابتة 🟡'
    if peak_pct > 0.40 and pnl_pct < peak_pct * 0.45:
        return 'الربح بيتراجع 🟠'
    return 'ضعفت 🔴'


def _maybe_position_review(pos: dict, market: dict) -> None:
    signal_id = str(pos.get('signal_id') or '')
    symbol = str(pos.get('symbol') or '')
    opened_at = float(pos.get('opened_at') or 0.0)
    entry = float(pos.get('entry') or 0.0)
    current = float(market.get('bid') or 0.0)
    if not signal_id or not symbol or opened_at <= 0 or entry <= 0 or current <= 0:
        return
    age = time.time() - opened_at
    peak = max(float(pos.get('peak_price') or entry), current)
    pnl_pct = ((current / entry) - 1.0) * 100.0
    peak_pct = ((peak / entry) - 1.0) * 100.0
    status = _position_status_label(pnl_pct, peak_pct)

    # If the service restarted after the deadline, send only the more useful 60m
    # message and mark the 45m review as satisfied to avoid a two-message burst.
    if age >= POSITION_REVIEW_60_SEC and not pos.get('deadline_60_sent_at'):
        text = (
            f'⏰ 60m DECISION DEADLINE\n{symbol}\n'
            f'⌛ Age: {int(age // 60)} min\n'
            f'💲 Current: {current:.8g}\n'
            f'📍 Entry ref: {entry:.8g}\n'
            f'📈 P/L ref: {pnl_pct:+.2f}%\n'
            f'🏔 Peak P/L: {peak_pct:+.2f}%\n'
            f'{status}\n\n'
            'الصفقة وصلت نهاية مدة FAST30_60 المستهدفة. '
            'الـOCO والـDynamic Profit Lock لسه شغالين للحماية.'
        )
        _alert(text)
        now = time.time()
        trade_state.update_position(signal_id, review_45_sent_at=pos.get('review_45_sent_at') or now, deadline_60_sent_at=now)
        trade_state.append_event('POSITION_DEADLINE_60_SENT', signal_id=signal_id, symbol=symbol, current=current, pnl_pct=pnl_pct, peak_pct=peak_pct)
        return

    if age >= POSITION_REVIEW_45_SEC and not pos.get('review_45_sent_at'):
        text = (
            f'⏱ 45m POSITION REVIEW\n{symbol}\n'
            f'⌛ Age: {int(age // 60)} min\n'
            f'💲 Current: {current:.8g}\n'
            f'📍 Entry ref: {entry:.8g}\n'
            f'📈 P/L ref: {pnl_pct:+.2f}%\n'
            f'🏔 Peak P/L: {peak_pct:+.2f}%\n'
            f'{status}\n\n'
            'مراجعة مبكرة. الـOCO والـDynamic Profit Lock شغالين؛ '
            'لو الربح بدأ يتاكل مدير الخروج يشد الحماية حسب الحركة.'
        )
        _alert(text)
        trade_state.update_position(signal_id, review_45_sent_at=time.time())
        trade_state.append_event('POSITION_REVIEW_45_SENT', signal_id=signal_id, symbol=symbol, current=current, pnl_pct=pnl_pct, peak_pct=peak_pct)


'''
if 'def _maybe_position_review(' not in s:
    if run_marker not in s:
        raise SystemExit('dynamic-exit-v2: run_once marker missing for reviews')
    s = s.replace(run_marker, review_helper + run_marker, 1)

market_line = "            market = _market(symbol)\n"
market_with_review = market_line + "            _maybe_position_review(pos, market)\n"
if '_maybe_position_review(pos, market)' not in s:
    if market_line not in s:
        raise SystemExit('dynamic-exit-v2: market marker missing for review call')
    s = s.replace(market_line, market_with_review, 1)

required = [
    'def _resolve_oco_client', 'OCO_RATCHET_RESPONSE_RECOVERED',
    'OCO_RATCHET_ROLLBACK_RESPONSE_RECOVERED', 'make-exact-oco-route',
    'def _cancel_make_oco(', 'CANCEL_OCO_EXACT',
    'cancelled, cancel = _cancel_exact_oco(pos, current_oid)',
    'POSITION_REVIEW_45_SEC =', 'POSITION_REVIEW_60_SEC =',
    'def _maybe_position_review(', '_maybe_position_review(pos, market)',
    '45m POSITION REVIEW', '60m DECISION DEADLINE', 'Peak P/L',
]
for item in required:
    if item not in s:
        raise SystemExit(f'dynamic-exit-v2: missing {item}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[dynamic-exit-v2-patch] OK exact OCO recovery + profit protection + 45m/60m Telegram lifecycle reviews')
