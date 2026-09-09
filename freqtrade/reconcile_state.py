from __future__ import annotations

"""Read-only Binance reconciliation for canonical live trade state.

This process never buys, sells, cancels, or replaces orders. It rebuilds bot-owned
open OCO positions after restart and closes local records when Binance reports the
order list finished. Bot ownership is identified only by TSTO-* OCO client IDs.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

import trade_state

API_BASES = [
    'https://api.binance.com',
    'https://api-gcp.binance.com',
    'https://api1.binance.com',
    'https://api2.binance.com',
    'https://api3.binance.com',
    'https://api4.binance.com',
]
POLL_SEC = max(30, int(os.getenv('RECONCILE_POLL_SEC', '60')))


def _creds() -> tuple[str, str]:
    key = (os.getenv('BINANCE_READ_API_KEY') or '').strip()
    secret = (os.getenv('BINANCE_READ_API_SECRET') or '').strip()
    if not key or not secret:
        raise RuntimeError('READ_ONLY_BINANCE_CREDENTIALS_MISSING')
    return key, secret


def _signed_get(path: str, params: dict):
    key, secret = _creds()
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    query = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    last = None
    for base in API_BASES:
        try:
            req = urllib.request.Request(
                f'{base}{path}?{query}&signature={sig}',
                headers={'X-MBX-APIKEY': key, 'User-Agent': 'tst-reconciler/1.0'},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read() or b'{}')
        except Exception as exc:
            last = exc
    raise RuntimeError(f'BINANCE_RECONCILE_READ_FAILED:{type(last).__name__}:{str(last)[:120]}')


def _order(symbol: str, order_id: int) -> dict:
    row = _signed_get('/api/v3/order', {'symbol': symbol, 'orderId': int(order_id)})
    return row if isinstance(row, dict) else {}


def _entry_from_recent_trades(symbol: str, around_ms: int, protected_qty: float) -> tuple[float, float, int | None]:
    rows = _signed_get('/api/v3/myTrades', {'symbol': symbol, 'limit': 100})
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in rows if isinstance(rows, list) else []:
        try:
            if not bool(row.get('isBuyer')):
                continue
            t = int(row.get('time') or 0)
            if t > around_ms + 120_000 or t < around_ms - 6 * 3600_000:
                continue
            groups[int(row.get('orderId'))].append(row)
        except Exception:
            continue
    candidates = []
    for oid, items in groups.items():
        qty = sum(float(x.get('qty') or 0) for x in items)
        quote = sum(float(x.get('qty') or 0) * float(x.get('price') or 0) for x in items)
        last_t = max(int(x.get('time') or 0) for x in items)
        if qty > 0 and quote > 0 and qty + 1e-12 >= protected_qty * 0.95:
            candidates.append((last_t, quote / qty, quote, oid))
    if not candidates:
        return 0.0, 0.0, None
    candidates.sort(reverse=True)
    _, entry, quote, oid = candidates[0]
    return entry, quote, oid


def _open_bot_ocos() -> list[dict]:
    rows = _signed_get('/api/v3/openOrderList', {})
    out = []
    for row in rows if isinstance(rows, list) else []:
        if str(row.get('listClientOrderId') or '').startswith('TSTO-'):
            out.append(row)
    return out


def _rebuild_unknown_open_oco(row: dict) -> bool:
    try:
        symbol = str(row.get('symbol') or '').upper()
        list_id = int(row.get('orderListId') or 0)
        trans_ms = int(row.get('transactionTime') or 0)
        orders = row.get('orders') or []
        if not symbol or list_id <= 0 or len(orders) < 2:
            return False
        details = [_order(symbol, int(x.get('orderId') or 0)) for x in orders if int(x.get('orderId') or 0) > 0]
        tp_order = next((x for x in details if str(x.get('type') or '').upper() in {'LIMIT_MAKER','LIMIT'}), None)
        stop_order = next((x for x in details if str(x.get('type') or '').upper() in {'STOP_LOSS_LIMIT','STOP_LOSS'}), None)
        if not tp_order or not stop_order:
            return False
        qty = min(float(tp_order.get('origQty') or 0), float(stop_order.get('origQty') or 0))
        tp = float(tp_order.get('price') or 0)
        stop = float(stop_order.get('stopPrice') or 0)
        stop_limit = float(stop_order.get('price') or stop)
        if qty <= 0 or tp <= 0 or stop <= 0:
            return False
        entry, quote, buy_order_id = _entry_from_recent_trades(symbol, trans_ms or int(time.time()*1000), qty)
        signal_id = f'REC-{symbol}-{list_id}'
        state = trade_state.load_state()
        if signal_id in state.get('positions', {}):
            return True
        state['positions'][signal_id] = {
            'signal_id': signal_id,
            'symbol': symbol,
            'entry': entry,
            'quantity': qty,
            'quote_spent': quote,
            'buy_order_id': buy_order_id,
            'target': tp,
            'stop': stop,
            'stop_limit': stop_limit,
            'oco_order_list_id': list_id,
            'status': 'OCO_ACTIVE',
            'opened_at': (trans_ms / 1000.0) if trans_ms else time.time(),
            'recovered_from_binance': True,
            'updated_at': time.time(),
        }
        trade_state.save_state(state)
        trade_state.append_event('POSITION_RECOVERED', signal_id=signal_id, symbol=symbol, order_list_id=list_id, entry=entry, quantity=qty, target=tp, stop=stop)
        print(f'[reconcile] RECOVERED {symbol} list={list_id} entry={entry:.10g} qty={qty:.10g}', flush=True)
        return True
    except Exception as exc:
        print(f'[reconcile] rebuild warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        return False


def run_once() -> None:
    open_ocos = _open_bot_ocos()
    open_ids = {int(x.get('orderListId') or 0) for x in open_ocos}
    state = trade_state.load_state()
    known_by_list = {
        int(v.get('oco_order_list_id') or 0): k
        for k, v in (state.get('positions') or {}).items()
        if isinstance(v, dict) and int(v.get('oco_order_list_id') or 0) > 0
    }

    for row in open_ocos:
        list_id = int(row.get('orderListId') or 0)
        if list_id not in known_by_list:
            _rebuild_unknown_open_oco(row)

    state = trade_state.load_state()
    for signal_id, pos in list((state.get('positions') or {}).items()):
        if not isinstance(pos, dict) or pos.get('status') != 'OCO_ACTIVE':
            continue
        oid = int(pos.get('oco_order_list_id') or 0)
        if oid <= 0 or oid in open_ids:
            continue
        try:
            row = _signed_get('/api/v3/orderList', {'orderListId': oid})
            status = str(row.get('listOrderStatus') or '').upper()
            status_type = str(row.get('listStatusType') or '').upper()
            if status in {'ALL_DONE','REJECT'} or status_type in {'ALL_DONE','RESPONSE'}:
                trade_state.update_position(signal_id, status='CLOSED', closed_at=time.time(), close_reason='BINANCE_RECONCILED_DONE')
                trade_state.append_event('POSITION_CLOSED', signal_id=signal_id, symbol=pos.get('symbol'), reason='BINANCE_RECONCILED_DONE', order_list_id=oid)
                print(f"[reconcile] CLOSED {pos.get('symbol')} list={oid}", flush=True)
        except Exception as exc:
            print(f"[reconcile] close-check warning {pos.get('symbol')} list={oid}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)

    snap = trade_state.portfolio_snapshot()
    print(f"[reconcile] OK bot_open_ocos={len(open_ocos)} tracked_open={snap['open_count']} risk={snap['stop_risk_usdt']:.4f} incomplete={snap['incomplete_count']}", flush=True)


def main() -> None:
    print(f'[reconcile] ONLINE read_only=True poll={POLL_SEC}s ownership_prefix=TSTO-', flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'[reconcile] loop warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    if '--once' in sys.argv:
        run_once()
    else:
        main()
