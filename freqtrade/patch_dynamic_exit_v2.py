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

# Make the write probe conservative: only explicit Binance unknown-order codes
# count as proof that TRADE permission was accepted.
required = ['def _resolve_oco_client', 'OCO_RATCHET_RESPONSE_RECOVERED', 'OCO_RATCHET_ROLLBACK_RESPONSE_RECOVERED']
for item in required:
    if item not in s:
        raise SystemExit(f'dynamic-exit-v2: missing {item}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[dynamic-exit-v2-patch] OK ambiguous OCO webhook responses are reconciled by deterministic list client id before rollback')
