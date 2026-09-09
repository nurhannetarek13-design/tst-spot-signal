from __future__ import annotations

"""Ownership-safe dynamic exit manager for Binance Spot OCO positions.

The manager never opens a BUY. It may only ratchet the stop upward on a tracked,
bot-owned OCO after verifying the exact Binance order list and that neither OCO
child has any executed quantity. Replacement is: exact cancel -> normalized new
OCO. If replacement fails it immediately attempts to restore the original OCO;
if restoration also fails the position becomes PROTECTION_PENDING and the
existing global fail-closed execution gate prevents fresh entries.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import binance_filters
import reconcile_state
import trade_state

PUBLIC_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']
API_BASES = ['https://api.binance.com', 'https://api-gcp.binance.com', 'https://api1.binance.com', 'https://api2.binance.com', 'https://api3.binance.com', 'https://api4.binance.com']
MAKE_OCO_URL = (os.getenv('MAKE_ONE_TAP_OCO_WEBHOOK_URL') or '').strip()
LIVE_ENABLED = (os.getenv('DYNAMIC_EXIT_LIVE', '0').strip() == '1')
POLL_SEC = max(10, int(os.getenv('DYNAMIC_EXIT_POLL_SEC', '20')))
MIN_RATCHET_GAP_SEC = max(60, int(os.getenv('DYNAMIC_EXIT_MIN_RATCHET_GAP_SEC', '120')))
TELEGRAM_BOT_TOKEN = (os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
TELEGRAM_CHAT_ID = (os.getenv('TELEGRAM_CHAT_ID') or '').strip()
_write_permission_cache = {'checked_at': 0.0, 'ok': False, 'reason': 'not-checked'}


def _alert(text: str) -> None:
    print(f'[dynamic-exit] ALERT {text}', flush=True)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        body = urllib.parse.urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'disable_web_page_preview': 'true'}).encode()
        req = urllib.request.Request(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage', data=body, method='POST')
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f'[dynamic-exit] alert warning {type(exc).__name__}: {str(exc)[:100]}', flush=True)


def _public(path: str, params: dict):
    q = urllib.parse.urlencode(params)
    last = None
    for base in PUBLIC_BASES:
        try:
            req = urllib.request.Request(f'{base}{path}?{q}', headers={'User-Agent': 'tst-dynamic-exit/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b'{}')
        except Exception as exc:
            last = exc
    raise RuntimeError(last or 'public-api-unavailable')


def _write_creds() -> tuple[str, str]:
    k = (os.getenv('BINANCE_API_KEY') or '').strip()
    s = (os.getenv('BINANCE_API_SECRET') or '').strip()
    if not k or not s:
        raise RuntimeError('BINANCE_WRITE_CREDENTIALS_MISSING')
    return k, s


def _signed_write(method: str, path: str, params: dict):
    key, secret = _write_creds()
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    query = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    last = None
    for base in API_BASES:
        req = urllib.request.Request(
            f'{base}{path}?{query}&signature={sig}', method=method,
            headers={'X-MBX-APIKEY': key, 'User-Agent': 'tst-dynamic-exit/1.0'},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as exc:
            data = exc.read() or b'{}'
            try:
                row = json.loads(data)
            except Exception:
                row = {'code': exc.code, 'msg': data.decode('utf-8', 'replace')[:160]}
            # Authentication/permission errors will not improve on another host.
            if int(row.get('code') or 0) in {-2014, -2015}:
                return exc.code, row
            last = (exc.code, row)
        except Exception as exc:
            last = (0, {'msg': f'{type(exc).__name__}:{str(exc)[:120]}'})
    if last:
        return last
    return 599, {'msg': 'write-request-failed'}


def _write_permission() -> tuple[bool, str]:
    now = time.time()
    if now - float(_write_permission_cache['checked_at']) < 600:
        return bool(_write_permission_cache['ok']), str(_write_permission_cache['reason'])
    if not LIVE_ENABLED:
        _write_permission_cache.update({'checked_at': now, 'ok': False, 'reason': 'live-disabled'})
        return False, 'live-disabled'
    # Safe permission probe: cancelling an intentionally impossible order id can
    # only return UNKNOWN_ORDER when TRADE write permission is accepted. It does
    # not create, fill, or cancel a real order.
    code, row = _signed_write('DELETE', '/api/v3/order', {'symbol': 'BTCUSDT', 'orderId': 922337203685477000})
    bcode = int(row.get('code') or 0) if isinstance(row, dict) else 0
    ok = bcode in {-2011, -2013}
    reason = 'trade-write-authorized' if ok else f'write-probe-failed-http{code}-code{bcode}'
    _write_permission_cache.update({'checked_at': now, 'ok': ok, 'reason': reason})
    return ok, reason


def _market(symbol: str) -> dict:
    book = _public('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0.0)
    ask = float(book.get('askPrice') or 0.0)
    if bid <= 0 or ask <= 0:
        raise RuntimeError('bad-book')
    rows = _public('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 45})
    now_ms = int(time.time() * 1000)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if len(rows) < 25:
        raise RuntimeError('insufficient-1m-bars')
    h = [float(x[2]) for x in rows]
    l = [float(x[3]) for x in rows]
    c = [float(x[4]) for x in rows]
    trs = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(rows))]
    atr = sum(trs[-14:]) / max(1, len(trs[-14:]))
    return {'bid': bid, 'ask': ask, 'high20': max(h[-20:]), 'swing_low7': min(l[-7:]), 'atr': atr}


def _owned_open_oco(pos: dict) -> tuple[bool, dict, list[dict]]:
    oid = int(pos.get('oco_order_list_id') or 0)
    symbol = str(pos.get('symbol') or '')
    if oid <= 0 or not symbol:
        return False, {}, []
    row = reconcile_state._signed_get('/api/v3/orderList', {'orderListId': oid})
    cid = str(row.get('listClientOrderId') or '')
    owned = cid.startswith('TSTO-') and str(row.get('symbol') or '') == symbol
    details = []
    for x in row.get('orders') or []:
        try:
            details.append(reconcile_state._order(symbol, int(x.get('orderId') or 0)))
        except Exception:
            pass
    # A partial/filled child means the state is in transition; never cancel it.
    if any(float(x.get('executedQty') or 0.0) > 0 for x in details):
        return False, row, details
    active = str(row.get('listOrderStatus') or '').upper() in {'EXECUTING', 'NEW'}
    return bool(owned and active), row, details


def _suggest_stop(pos: dict, market: dict) -> tuple[float | None, str]:
    entry = float(pos.get('entry') or 0.0)
    stop = float(pos.get('stop') or 0.0)
    target = float(pos.get('target') or 0.0)
    peak = max(float(pos.get('peak_price') or entry), float(market['high20']), float(market['bid']))
    if not (entry > stop > 0 and target > entry and peak > entry):
        return None, 'not-ready'
    r0 = float(pos.get('initial_risk_per_unit') or max(entry - stop, entry * 0.001))
    r_mult = (peak - entry) / max(r0, 1e-12)
    progress = (peak - entry) / max(target - entry, 1e-12)
    suggested = stop
    reason = 'hold'
    if r_mult >= 1.0 or progress >= 0.45:
        suggested = max(suggested, entry * 1.0015)
        reason = 'breakeven-plus'
    if r_mult >= 1.55 or progress >= 0.72:
        structure = float(market['swing_low7']) - 0.20 * float(market['atr'])
        suggested = max(suggested, min(structure, float(market['bid']) * 0.996), entry + 0.65 * r0)
        reason = 'structure-lock'
    if r_mult >= 2.0 or progress >= 0.88:
        structure = float(market['swing_low7']) - 0.12 * float(market['atr'])
        suggested = max(suggested, min(structure, float(market['bid']) * 0.995), entry + 1.10 * r0)
        reason = 'near-target-lock'
    suggested = min(suggested, float(market['bid']) * 0.994)
    if suggested <= stop * 1.002:
        return None, 'no-material-ratchet'
    return suggested, reason


def _ids(signal_id: str, seq: int) -> tuple[str, str, str]:
    d = hashlib.sha256(f'{signal_id}:{seq}'.encode()).hexdigest()[:22]
    return f'TSTO-R{d}', f'TSTS-R{d}', f'TSTL-R{d}'


def _place_make_oco(pos: dict, quantity: float, target: float, stop: float, seq: int) -> tuple[bool, dict, dict]:
    if not MAKE_OCO_URL:
        return False, {'status': 'MAKE_OCO_URL_MISSING'}, {}
    symbol = str(pos.get('symbol') or '')
    stop_limit = stop * 0.997
    normalized, meta = binance_filters.normalize_oco(symbol, quantity, target, stop, stop_limit)
    list_id, stop_id, limit_id = _ids(str(pos.get('signal_id') or ''), seq)
    body = {
        'signal_id': str(pos.get('signal_id') or ''), 'action': 'OCO', 'symbol': symbol,
        'quantity': normalized['quantity'], 'take_profit_price': normalized['take_profit_price'],
        'stop_loss_price': normalized['stop_loss_price'], 'stop_limit_price': normalized['stop_limit_price'],
        'list_client_order_id': list_id, 'stop_client_order_id': stop_id, 'limit_client_order_id': limit_id,
        'confirmed': True, 'dry_run': False, 'timestamp': int(time.time()),
    }
    raw = json.dumps(body, separators=(',', ':')).encode()
    req = urllib.request.Request(MAKE_OCO_URL, data=raw, method='POST', headers={'Content-Type': 'application/json', 'User-Agent': 'tst-dynamic-exit/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            row = json.loads(r.read() or b'{}')
            return bool(r.status < 300 and row.get('ok') is True and str(row.get('status')) == 'OCO_PLACED'), row, {**body, 'filter_meta': meta}
    except urllib.error.HTTPError as exc:
        try:
            row = json.loads(exc.read() or b'{}')
        except Exception:
            row = {'status': f'HTTP_{exc.code}'}
        return False, row, {**body, 'filter_meta': meta}
    except Exception as exc:
        return False, {'status': f'{type(exc).__name__}:{str(exc)[:120]}'}, {**body, 'filter_meta': meta}


def _record_new_oco(pos: dict, body: dict, row: dict, reason: str) -> None:
    signal_id = str(pos.get('signal_id') or '')
    oid = int(row.get('oco_order_list_id') or 0)
    if oid <= 0:
        raise RuntimeError('new-oco-id-missing')
    trade_state.update_position(
        signal_id,
        status='OCO_ACTIVE', oco_order_list_id=oid,
        oco_list_client_order_id=body.get('list_client_order_id'),
        oco_stop_client_order_id=body.get('stop_client_order_id'),
        oco_limit_client_order_id=body.get('limit_client_order_id'),
        target=float(body['take_profit_price']), stop=float(body['stop_loss_price']),
        stop_limit=float(body['stop_limit_price']), last_live_ratchet_at=time.time(),
        last_live_ratchet_reason=reason,
    )
    trade_state.update_reservation(signal_id, 'OCO', status='PLACED', response=row, placed_at=time.time(), replacement=True)


def _replace(pos: dict, suggested: float, reason: str, market: dict) -> None:
    signal_id = str(pos.get('signal_id') or '')
    symbol = str(pos.get('symbol') or '')
    current_oid = int(pos.get('oco_order_list_id') or 0)
    quantity = float(pos.get('quantity') or 0.0)
    target = float(pos.get('target') or 0.0)
    old_stop = float(pos.get('stop') or 0.0)
    seq = int(pos.get('live_ratchet_seq') or 0) + 1
    ok, _, _ = _owned_open_oco(pos)
    if not ok:
        return
    can_write, why = _write_permission()
    if not can_write:
        trade_state.update_position(signal_id, shadow_suggested_stop=suggested, shadow_reason=reason, shadow_suggested_at=time.time())
        print(f'[dynamic-exit] SHADOW {symbol} stop={old_stop:.10g}->{suggested:.10g} reason={reason} write={why}', flush=True)
        return
    if time.time() - float(pos.get('last_live_ratchet_at') or 0.0) < MIN_RATCHET_GAP_SEC:
        return

    # Re-check ownership immediately before the only destructive call.
    owned, _, details = _owned_open_oco(pos)
    if not owned or any(float(x.get('executedQty') or 0.0) > 0 for x in details):
        return

    code, cancel = _signed_write('DELETE', '/api/v3/orderList', {'symbol': symbol, 'orderListId': current_oid})
    if code >= 300 or int(cancel.get('orderListId') or 0) != current_oid:
        trade_state.append_event('OCO_RATCHET_CANCEL_FAILED', signal_id=signal_id, symbol=symbol, order_list_id=current_oid, response=cancel)
        return

    trade_state.update_position(signal_id, status='PROTECTION_PENDING', protection_lost_at=time.time(), live_ratchet_seq=seq)
    trade_state.append_event('OCO_RATCHET_CANCELLED', signal_id=signal_id, symbol=symbol, old_order_list_id=current_oid, old_stop=old_stop, suggested_stop=suggested, reason=reason)

    placed, row, body = _place_make_oco(pos, quantity, target, suggested, seq)
    if placed:
        _record_new_oco(pos, body, row, reason)
        trade_state.update_position(signal_id, live_ratchet_seq=seq, peak_price=max(float(pos.get('peak_price') or 0.0), float(market['high20'])))
        trade_state.append_event('OCO_RATCHET_LIVE', signal_id=signal_id, symbol=symbol, old_order_list_id=current_oid, new_order_list_id=row.get('oco_order_list_id'), old_stop=old_stop, new_stop=body.get('stop_loss_price'), target=body.get('take_profit_price'), reason=reason)
        print(f"[dynamic-exit] LIVE_RATCHET {symbol} stop={old_stop:.10g}->{float(body['stop_loss_price']):.10g} list={row.get('oco_order_list_id')} reason={reason}", flush=True)
        return

    # Immediate rollback: restore original stop/target with a fresh client id.
    rescue, rescue_row, rescue_body = _place_make_oco(pos, quantity, target, old_stop, seq + 100000)
    if rescue:
        _record_new_oco(pos, rescue_body, rescue_row, 'ratchet-rollback')
        trade_state.update_position(signal_id, live_ratchet_seq=seq)
        trade_state.append_event('OCO_RATCHET_ROLLBACK_OK', signal_id=signal_id, symbol=symbol, new_order_list_id=rescue_row.get('oco_order_list_id'), failed_response=row)
        _alert(f'⚠️ {symbol}: dynamic stop replacement failed; original protection was restored.')
    else:
        trade_state.update_position(signal_id, status='PROTECTION_PENDING', protection_lost_at=time.time(), live_ratchet_seq=seq)
        trade_state.update_reservation(signal_id, 'OCO', status='REPLACE_FAILED', failed_response=row, rescue_response=rescue_row, reconciled_at=time.time())
        trade_state.append_event('OCO_RATCHET_ROLLBACK_FAILED', signal_id=signal_id, symbol=symbol, failed_response=row, rescue_response=rescue_row)
        _alert(f'🚨 {symbol}: OCO replacement AND rollback failed. Position is unprotected; new entries are blocked.')


def run_once() -> None:
    can_write, write_reason = _write_permission()
    for pos in trade_state.open_positions():
        if str(pos.get('status') or '') != 'OCO_ACTIVE':
            continue
        signal_id = str(pos.get('signal_id') or '')
        symbol = str(pos.get('symbol') or '')
        if not signal_id or not symbol:
            continue
        try:
            owned, _, details = _owned_open_oco(pos)
            if not owned:
                continue
            if any(float(x.get('executedQty') or 0.0) > 0 for x in details):
                continue
            market = _market(symbol)
            peak = max(float(pos.get('peak_price') or pos.get('entry') or 0.0), float(market['high20']), float(market['bid']))
            changes = {'peak_price': peak, 'last_bid': float(market['bid']), 'last_market_check': time.time()}
            if not pos.get('initial_risk_per_unit'):
                changes['initial_risk_per_unit'] = max(0.0, float(pos.get('entry') or 0.0) - float(pos.get('stop') or 0.0))
            trade_state.update_position(signal_id, **changes)
            pos = {**pos, **changes}
            suggested, reason = _suggest_stop(pos, market)
            if suggested is None:
                continue
            if LIVE_ENABLED and can_write:
                _replace(pos, suggested, reason, market)
            else:
                trade_state.update_position(signal_id, shadow_suggested_stop=suggested, shadow_reason=reason, shadow_suggested_at=time.time())
                print(f"[dynamic-exit] SHADOW {symbol} stop={float(pos.get('stop') or 0):.10g}->{suggested:.10g} reason={reason} write={write_reason}", flush=True)
        except Exception as exc:
            print(f'[dynamic-exit] {symbol} warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)


def main() -> None:
    ok, why = _write_permission()
    print(f'[dynamic-exit] ONLINE live_requested={LIVE_ENABLED} write_permission={ok} reason={why} exact_ownership=True rollback=True poll={POLL_SEC}s', flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'[dynamic-exit] loop warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
