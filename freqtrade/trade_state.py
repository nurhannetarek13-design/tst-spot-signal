from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Canonical live state defaults to Railway's persistent /data volume.
STATE_PATH = Path(os.getenv('TST_TRADE_STATE_PATH', '/data/tst_live_positions.json'))
EVENT_PATH = Path(os.getenv('TST_TRADE_EVENT_PATH', '/data/tst_trade_events.jsonl'))
RESERVATION_PATH = Path(os.getenv('TST_EXECUTION_RESERVATION_PATH', '/data/tst_execution_reservations.json'))
_LOCK = threading.RLock()


def _empty() -> dict[str, Any]:
    return {'version': 2, 'updated_at': time.time(), 'positions': {}}


def _empty_reservations() -> dict[str, Any]:
    return {'version': 1, 'updated_at': time.time(), 'reservations': {}}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, separators=(',', ':'), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


def load_state() -> dict[str, Any]:
    with _LOCK:
        try:
            row = json.loads(STATE_PATH.read_text(encoding='utf-8'))
            if isinstance(row, dict) and isinstance(row.get('positions'), dict):
                return row
        except Exception:
            pass
        return _empty()


def save_state(state: dict[str, Any]) -> None:
    with _LOCK:
        state['updated_at'] = time.time()
        _atomic_write(STATE_PATH, state)


def load_reservations() -> dict[str, Any]:
    with _LOCK:
        try:
            row = json.loads(RESERVATION_PATH.read_text(encoding='utf-8'))
            if isinstance(row, dict) and isinstance(row.get('reservations'), dict):
                return row
        except Exception:
            pass
        return _empty_reservations()


def save_reservations(state: dict[str, Any]) -> None:
    with _LOCK:
        state['updated_at'] = time.time()
        _atomic_write(RESERVATION_PATH, state)


def append_event(kind: str, **data: Any) -> None:
    with _LOCK:
        EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {'ts': time.time(), 'kind': kind, **data}
        with EVENT_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())


def _signal_id(body: dict[str, Any]) -> str:
    return str(body.get('signal_id') or body.get('id') or '').strip()


def reserve_execution(body: dict[str, Any], action: str) -> tuple[bool, dict[str, Any]]:
    """Atomically reserve a signal/action before any external side effect.

    Duplicate clicks/webhook retries are blocked before they can create another
    order. UNKNOWN reservations are not automatically re-sent; reconciliation
    must decide whether Binance accepted the first request.
    """
    signal_id = _signal_id(body)
    if not signal_id:
        return False, {'status': 'MISSING_SIGNAL_ID'}
    key = f'{signal_id}:{action.upper()}'
    with _LOCK:
        state = load_reservations()
        current = state['reservations'].get(key)
        if isinstance(current, dict):
            return False, dict(current)
        row = {
            'key': key,
            'signal_id': signal_id,
            'action': action.upper(),
            'symbol': str(body.get('symbol') or '').upper(),
            'quote_amount_usdt': body.get('quote_amount_usdt'),
            'quantity': body.get('quantity'),
            'status': 'RESERVED',
            'reserved_at': time.time(),
            'updated_at': time.time(),
        }
        state['reservations'][key] = row
        save_reservations(state)
    append_event('EXECUTION_RESERVED', **row)
    return True, row


def update_reservation(signal_id: str, action: str, **changes: Any) -> dict[str, Any] | None:
    key = f'{signal_id}:{action.upper()}'
    with _LOCK:
        state = load_reservations()
        row = state['reservations'].get(key)
        if not isinstance(row, dict):
            return None
        row.update(changes)
        row['updated_at'] = time.time()
        state['reservations'][key] = row
        save_reservations(state)
        return dict(row)


def get_reservation(signal_id: str, action: str) -> dict[str, Any] | None:
    row = (load_reservations().get('reservations') or {}).get(f'{signal_id}:{action.upper()}')
    return dict(row) if isinstance(row, dict) else None


def record_buy(body: dict[str, Any], response: dict[str, Any]) -> None:
    signal_id = _signal_id(body) or str(response.get('signal_id') or '').strip()
    if not signal_id:
        return
    try:
        qty = float(response.get('executed_qty') or 0)
        quote = float(response.get('quote_spent') or 0)
    except Exception:
        qty = quote = 0.0
    if qty <= 0 or quote <= 0:
        return
    entry = quote / qty
    symbol = str(response.get('symbol') or body.get('symbol') or '').upper()
    with _LOCK:
        state = load_state()
        pos = state['positions'].get(signal_id, {})
        pos.update({
            'signal_id': signal_id,
            'symbol': symbol,
            'entry': entry,
            'quantity': qty,
            'quote_spent': quote,
            'buy_order_id': response.get('order_id'),
            'status': 'BUY_FILLED',
            'opened_at': pos.get('opened_at') or time.time(),
            'updated_at': time.time(),
        })
        state['positions'][signal_id] = pos
        save_state(state)
    update_reservation(signal_id, 'BUY', status='FILLED', response=response, filled_at=time.time())
    append_event('BUY_FILLED', signal_id=signal_id, symbol=symbol, entry=entry, quantity=qty, quote_spent=quote, order_id=response.get('order_id'))


def record_oco(body: dict[str, Any], response: dict[str, Any]) -> None:
    signal_id = _signal_id(body) or str(response.get('signal_id') or '').strip()
    if not signal_id:
        return
    with _LOCK:
        state = load_state()
        pos = state['positions'].get(signal_id, {})
        try:
            qty = float(body.get('quantity') or pos.get('quantity') or 0)
            tp = float(body.get('take_profit_price') or 0)
            sl = float(body.get('stop_loss_price') or 0)
            sl_limit = float(body.get('stop_limit_price') or 0)
            order_list_id = int(float(response.get('oco_order_list_id') or response.get('orderListId') or 0))
        except Exception:
            return
        if qty <= 0 or tp <= sl or sl <= 0 or order_list_id <= 0:
            return
        symbol = str(body.get('symbol') or response.get('symbol') or pos.get('symbol') or '').upper()
        pos.update({
            'signal_id': signal_id,
            'symbol': symbol,
            'quantity': qty,
            'target': tp,
            'stop': sl,
            'stop_limit': sl_limit,
            'oco_order_list_id': order_list_id,
            'status': 'OCO_ACTIVE',
            'oco_updated_at': time.time(),
            'updated_at': time.time(),
        })
        if body.get('model_take_profit_price') is not None:
            try:
                pos['model_target'] = float(body.get('model_take_profit_price'))
            except Exception:
                pass
        state['positions'][signal_id] = pos
        save_state(state)
    update_reservation(signal_id, 'OCO', status='PLACED', response=response, placed_at=time.time())
    append_event('OCO_ACTIVE', signal_id=signal_id, symbol=symbol, order_list_id=order_list_id, quantity=qty, target=tp, stop=sl)


def update_position(signal_id: str, **changes: Any) -> None:
    with _LOCK:
        state = load_state()
        pos = state['positions'].get(signal_id)
        if not isinstance(pos, dict):
            return
        pos.update(changes)
        pos['updated_at'] = time.time()
        state['positions'][signal_id] = pos
        save_state(state)


def position_for_signal(signal_id: str) -> dict[str, Any] | None:
    pos = (load_state().get('positions') or {}).get(signal_id)
    return dict(pos) if isinstance(pos, dict) else None


def open_positions() -> list[dict[str, Any]]:
    state = load_state()
    return [dict(v) for v in state.get('positions', {}).values() if isinstance(v, dict) and v.get('status') in {'BUY_FILLED', 'OCO_ACTIVE', 'PROTECTION_PENDING'}]


def portfolio_snapshot() -> dict[str, Any]:
    positions = open_positions()
    risk = 0.0
    incomplete = 0
    for pos in positions:
        try:
            entry = float(pos.get('entry') or 0)
            stop = float(pos.get('stop') or 0)
            qty = float(pos.get('quantity') or 0)
            if entry > 0 and stop > 0 and qty > 0:
                risk += max(0.0, entry - stop) * qty
            else:
                incomplete += 1
        except Exception:
            incomplete += 1
    return {'open_count': len(positions), 'stop_risk_usdt': risk, 'incomplete_count': incomplete}
