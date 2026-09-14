from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import dynamic_exit_manager as dex
import reconcile_state
import trade_state

POLL_SEC = max(10, int(os.getenv('HARD_TIME_EXIT_POLL_SEC', '15')))
MAX_HOLD_SEC = max(900, int(os.getenv('HARD_TIME_EXIT_SEC', '3600')))
RETRY_SEC = max(120, int(os.getenv('HARD_TIME_EXIT_RETRY_SEC', '300')))
PENDING_RECOVERY_SEC = max(20, int(os.getenv('HARD_TIME_EXIT_PENDING_RECOVERY_SEC', '45')))
ENABLED = os.getenv('HARD_TIME_EXIT_ENABLED', '1').strip() == '1'


def _client_id(signal_id: str, attempt: int) -> str:
    digest = hashlib.sha256(f'{signal_id}:TIME_EXIT:{attempt}'.encode()).hexdigest()[:24]
    return f'TSTX-{digest}'


def _query_sell(symbol: str, client_order_id: str) -> dict | None:
    if not symbol or not client_order_id:
        return None
    try:
        row = reconcile_state._signed_get(
            '/api/v3/order',
            {'symbol': symbol, 'origClientOrderId': client_order_id},
        )
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    if str(row.get('symbol') or '') != symbol:
        return None
    if str(row.get('side') or '').upper() != 'SELL':
        return None
    if str(row.get('clientOrderId') or '') != client_order_id:
        return None
    return row


def _post_market_sell(pos: dict, client_order_id: str) -> dict:
    if not dex.MAKE_OCO_URL:
        return {'status': 'MAKE_OCO_URL_MISSING'}
    body = {
        'signal_id': str(pos.get('signal_id') or ''),
        'action': 'SELL',
        'symbol': str(pos.get('symbol') or ''),
        'quantity': float(pos.get('quantity') or 0.0),
        'client_order_id': client_order_id,
        'confirmed': True,
        'dry_run': False,
        'timestamp': int(time.time()),
    }
    raw = json.dumps(body, separators=(',', ':')).encode()
    req = urllib.request.Request(
        dex.MAKE_OCO_URL,
        data=raw,
        method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'tst-hard-time-exit/1.0'},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            row = json.loads(r.read() or b'{}')
            return row if isinstance(row, dict) else {'status': 'INVALID_MAKE_RESPONSE'}
    except urllib.error.HTTPError as exc:
        try:
            row = json.loads(exc.read() or b'{}')
            return row if isinstance(row, dict) else {'status': f'HTTP_{exc.code}'}
        except Exception:
            return {'status': f'HTTP_{exc.code}'}
    except Exception as exc:
        return {'status': f'{type(exc).__name__}:{str(exc)[:120]}'}


def _close_from_sell(pos: dict, order: dict) -> bool:
    try:
        if str(order.get('side') or '').upper() != 'SELL':
            return False
        if str(order.get('status') or '').upper() != 'FILLED':
            return False
        qty = float(order.get('executedQty') or 0.0)
        quote = float(order.get('cummulativeQuoteQty') or 0.0)
        if qty <= 0 or quote <= 0:
            return False
        exit_price = quote / qty
        entry = float(pos.get('entry') or 0.0)
        cost_basis = entry * qty if entry > 0 else 0.0
        pnl = quote - cost_basis - (cost_basis * reconcile_state.PNL_COST_BUFFER_PCT) if cost_basis > 0 else 0.0
        signal_id = str(pos.get('signal_id') or '')
        trade_state.close_position(
            signal_id,
            exit_price=exit_price,
            exit_qty=qty,
            exit_quote=quote,
            realized_pnl_usdt=pnl,
            close_reason='TIME_EXIT',
            exit_order_id=int(order.get('orderId') or 0) or None,
            closed_at=(float(order.get('updateTime') or order.get('time') or 0) / 1000.0) or time.time(),
            exit_order_type=str(order.get('type') or 'MARKET'),
            exit_binance_status=order.get('status'),
            pnl_cost_buffer_pct=reconcile_state.PNL_COST_BUFFER_PCT,
            time_exit=True,
        )
        trade_state.append_event(
            'TIME_EXIT_FILLED',
            signal_id=signal_id,
            symbol=pos.get('symbol'),
            exit_price=exit_price,
            exit_qty=qty,
            exit_quote=quote,
            realized_pnl_usdt=pnl,
            order_id=order.get('orderId'),
        )
        print(
            f"[hard-time-exit] CLOSED {pos.get('symbol')} price={exit_price:.10g} pnl≈{pnl:+.4f}USDT",
            flush=True,
        )
        return True
    except Exception as exc:
        print(f'[hard-time-exit] close warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        return False


def _rescue_oco(pos: dict, reason: str) -> bool:
    signal_id = str(pos.get('signal_id') or '')
    symbol = str(pos.get('symbol') or '')
    quantity = float(pos.get('quantity') or 0.0)
    target = float(pos.get('target') or 0.0)
    stop = float(pos.get('stop') or 0.0)
    if quantity <= 0 or target <= 0 or stop <= 0:
        return False
    seq = int(pos.get('live_ratchet_seq') or 0) + 700000 + int(pos.get('time_exit_attempt') or 0)
    placed, row, body = dex._place_make_oco(pos, quantity, target, stop, seq)
    if not placed and body.get('list_client_order_id'):
        resolved = dex._resolve_oco_client(symbol, str(body.get('list_client_order_id') or ''))
        if resolved:
            placed, row = True, resolved
    if not placed:
        trade_state.update_position(
            signal_id,
            status='PROTECTION_PENDING',
            time_exit_last_error=reason,
            time_exit_retry_after=time.time() + RETRY_SEC,
        )
        dex._alert(f'🚨 {symbol}: 60m exit failed and OCO rescue failed. New entries stay blocked until protection is restored.')
        return False
    dex._record_new_oco(pos, body, row, 'time-exit-rollback')
    trade_state.update_position(
        signal_id,
        time_exit_last_error=reason,
        time_exit_retry_after=time.time() + RETRY_SEC,
        time_exit_pending_at=None,
    )
    trade_state.append_event(
        'TIME_EXIT_ROLLBACK_OCO_OK',
        signal_id=signal_id,
        symbol=symbol,
        order_list_id=row.get('oco_order_list_id'),
        reason=reason,
    )
    dex._alert(f'⚠️ {symbol}: 60m market exit was not confirmed; OCO protection was restored and the exit will retry safely.')
    return True


def _recover_pending(pos: dict) -> bool:
    signal_id = str(pos.get('signal_id') or '')
    symbol = str(pos.get('symbol') or '')
    client_id = str(pos.get('time_exit_client_order_id') or '')
    if not client_id:
        return False
    order = _query_sell(symbol, client_id)
    if order and _close_from_sell(pos, order):
        return True
    pending_at = float(pos.get('time_exit_pending_at') or 0.0)
    if pending_at > 0 and time.time() - pending_at >= PENDING_RECOVERY_SEC:
        _rescue_oco(pos, 'time-exit-unconfirmed-after-cancel')
        return True
    return False


def _attempt_exit(pos: dict) -> None:
    signal_id = str(pos.get('signal_id') or '')
    symbol = str(pos.get('symbol') or '')
    oid = int(pos.get('oco_order_list_id') or 0)
    quantity = float(pos.get('quantity') or 0.0)
    if not signal_id or not symbol or oid <= 0 or quantity <= 0:
        return

    retry_after = float(pos.get('time_exit_retry_after') or 0.0)
    if retry_after > time.time():
        return

    attempt = int(pos.get('time_exit_attempt') or 0) + 1
    client_id = _client_id(signal_id, attempt)
    trade_state.update_position(
        signal_id,
        time_exit_attempt=attempt,
        time_exit_client_order_id=client_id,
        time_exit_pending_at=time.time(),
        time_exit_retry_after=0.0,
    )

    owned, _, details = dex._owned_open_oco(pos)
    if not owned or any(float(x.get('executedQty') or 0.0) > 0 for x in details):
        trade_state.update_position(signal_id, time_exit_pending_at=None)
        return

    cancelled, cancel_row = dex._cancel_exact_oco(pos, oid)
    if not cancelled:
        trade_state.update_position(
            signal_id,
            time_exit_pending_at=None,
            time_exit_last_error='oco-cancel-failed',
            time_exit_retry_after=time.time() + RETRY_SEC,
        )
        trade_state.append_event(
            'TIME_EXIT_CANCEL_FAILED',
            signal_id=signal_id,
            symbol=symbol,
            order_list_id=oid,
            response=cancel_row,
        )
        return

    trade_state.update_position(
        signal_id,
        status='PROTECTION_PENDING',
        time_exit_oco_cancelled_at=time.time(),
        time_exit_oco_cancelled_id=oid,
    )
    trade_state.append_event('TIME_EXIT_OCO_CANCELLED', signal_id=signal_id, symbol=symbol, order_list_id=oid)

    response = _post_market_sell(pos, client_id)
    order = None
    try:
        order_id = int(float(response.get('order_id') or response.get('orderId') or 0))
    except Exception:
        order_id = 0
    if order_id > 0:
        try:
            order = reconcile_state._order(symbol, order_id)
        except Exception:
            order = None
    if order is None:
        order = _query_sell(symbol, client_id)

    if order and _close_from_sell(pos, order):
        return

    _rescue_oco(pos, f"market-sell-unconfirmed:{str(response.get('status') or 'unknown')[:80]}")


def run_once() -> None:
    now = time.time()
    state = trade_state.load_state()
    for pos in list((state.get('positions') or {}).values()):
        if not isinstance(pos, dict):
            continue
        status = str(pos.get('status') or '')
        if status == 'CLOSED':
            continue
        signal_id = str(pos.get('signal_id') or '')
        opened_at = float(pos.get('opened_at') or 0.0)
        if not signal_id or opened_at <= 0 or now - opened_at < MAX_HOLD_SEC:
            continue

        if status == 'PROTECTION_PENDING' and pos.get('time_exit_client_order_id'):
            _recover_pending(pos)
            continue
        if status != 'OCO_ACTIVE':
            continue

        existing_client = str(pos.get('time_exit_client_order_id') or '')
        if existing_client:
            order = _query_sell(str(pos.get('symbol') or ''), existing_client)
            if order and _close_from_sell(pos, order):
                continue
        _attempt_exit(pos)


def main() -> None:
    print(
        f'[hard-time-exit] ONLINE enabled={ENABLED} max_hold={MAX_HOLD_SEC}s poll={POLL_SEC}s '
        f'ownership_safe=True cancel_before_sell=True rollback_oco=True',
        flush=True,
    )
    while True:
        try:
            if ENABLED:
                run_once()
        except Exception as exc:
            print(f'[hard-time-exit] loop warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
