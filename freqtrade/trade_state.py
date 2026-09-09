from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Canonical live state defaults to Railway's persistent /data volume.
STATE_PATH = Path(os.getenv('TST_TRADE_STATE_PATH', '/data/tst_live_positions.json'))
EVENT_PATH = Path(os.getenv('TST_TRADE_EVENT_PATH', '/data/tst_trade_events.jsonl'))
RESERVATION_PATH = Path(os.getenv('TST_EXECUTION_RESERVATION_PATH', '/data/tst_execution_reservations.json'))
RISK_TIMEZONE = os.getenv('RISK_TIMEZONE', 'Africa/Cairo')
_LOCK = threading.RLock()


def _empty() -> dict[str, Any]:
    return {'version': 4, 'updated_at': time.time(), 'positions': {}}


def _empty_reservations() -> dict[str, Any]:
    return {'version': 2, 'updated_at': time.time(), 'reservations': {}}


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
            'take_profit_price': body.get('take_profit_price'),
            'model_take_profit_price': body.get('model_take_profit_price'),
            'stop_loss_price': body.get('stop_loss_price'),
            'stop_limit_price': body.get('stop_limit_price'),
            'signal_timestamp': body.get('timestamp'),
            'client_order_id': body.get('client_order_id'),
            'list_client_order_id': body.get('list_client_order_id'),
            'stop_client_order_id': body.get('stop_client_order_id'),
            'limit_client_order_id': body.get('limit_client_order_id'),
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


def reservations(action: str | None = None, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    rows=[]
    wanted={x.upper() for x in statuses} if statuses else None
    for row in (load_reservations().get('reservations') or {}).values():
        if not isinstance(row,dict):
            continue
        if action and str(row.get('action') or '').upper()!=action.upper():
            continue
        if wanted and str(row.get('status') or '').upper() not in wanted:
            continue
        rows.append(dict(row))
    return rows


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
            'buy_client_order_id': body.get('client_order_id') or response.get('client_order_id'),
            'target': body.get('take_profit_price'),
            'model_target': body.get('model_take_profit_price'),
            'stop': body.get('stop_loss_price'),
            'status': 'BUY_FILLED',
            'opened_at': pos.get('opened_at') or time.time(),
            'updated_at': time.time(),
        })
        state['positions'][signal_id] = pos
        save_state(state)
    update_reservation(signal_id, 'BUY', status='FILLED', response=response, filled_at=time.time())
    append_event('BUY_FILLED', signal_id=signal_id, symbol=symbol, entry=entry, quantity=qty, quote_spent=quote, order_id=response.get('order_id'), client_order_id=body.get('client_order_id'))


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
            'oco_list_client_order_id': body.get('list_client_order_id'),
            'oco_stop_client_order_id': body.get('stop_client_order_id'),
            'oco_limit_client_order_id': body.get('limit_client_order_id'),
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
    append_event('OCO_ACTIVE', signal_id=signal_id, symbol=symbol, order_list_id=order_list_id, list_client_order_id=body.get('list_client_order_id'), quantity=qty, target=tp, stop=sl)


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


def close_position(signal_id: str, *, exit_price: float, exit_qty: float, exit_quote: float,
                   realized_pnl_usdt: float, close_reason: str, exit_order_id: int | None = None,
                   closed_at: float | None = None, **extra: Any) -> None:
    """Persist a reconciled close and its conservative net-PnL estimate."""
    when=float(closed_at or time.time())
    with _LOCK:
        state=load_state(); pos=state.get('positions',{}).get(signal_id)
        if not isinstance(pos,dict):
            return
        pos.update({
            'status':'CLOSED','closed_at':when,'close_reason':close_reason,
            'exit_price':float(exit_price),'exit_quantity':float(exit_qty),'exit_quote':float(exit_quote),
            'realized_pnl_usdt':float(realized_pnl_usdt),'exit_order_id':exit_order_id,
            'updated_at':time.time(), **extra,
        })
        state['positions'][signal_id]=pos; save_state(state)
    append_event('POSITION_CLOSED', signal_id=signal_id, symbol=pos.get('symbol'), reason=close_reason,
                 exit_price=exit_price, exit_quantity=exit_qty, exit_quote=exit_quote,
                 realized_pnl_usdt=realized_pnl_usdt, exit_order_id=exit_order_id)


def position_for_signal(signal_id: str) -> dict[str, Any] | None:
    pos = (load_state().get('positions') or {}).get(signal_id)
    return dict(pos) if isinstance(pos, dict) else None


def open_positions() -> list[dict[str, Any]]:
    state = load_state()
    return [dict(v) for v in state.get('positions', {}).values() if isinstance(v, dict) and v.get('status') in {'BUY_FILLED', 'OCO_ACTIVE', 'PROTECTION_PENDING'}]


def closed_positions() -> list[dict[str, Any]]:
    state=load_state()
    rows=[dict(v) for v in state.get('positions',{}).values() if isinstance(v,dict) and v.get('status')=='CLOSED' and v.get('realized_pnl_usdt') is not None]
    rows.sort(key=lambda x:float(x.get('closed_at') or 0))
    return rows


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


def performance_snapshot(now: float | None = None) -> dict[str, Any]:
    """Actual reconciled bot results used by risk circuit breakers.

    Uses the configured local risk day (Africa/Cairo by default), not candle
    outcomes or theoretical signals. Drawdown is measured on cumulative realized
    bot PnL so a restart cannot reset it.
    """
    t=float(now or time.time())
    try: tz=ZoneInfo(RISK_TIMEZONE)
    except Exception: tz=ZoneInfo('UTC')
    today=datetime.fromtimestamp(t,tz).date()
    rows=closed_positions()
    today_rows=[]
    for p in rows:
        try:
            d=datetime.fromtimestamp(float(p.get('closed_at') or 0),tz).date()
            if d==today: today_rows.append(p)
        except Exception: pass
    realized_today=sum(float(p.get('realized_pnl_usdt') or 0) for p in today_rows)

    consecutive_losses=0
    for p in reversed(rows):
        if float(p.get('realized_pnl_usdt') or 0)<0: consecutive_losses+=1
        else: break

    cumulative=0.0; peak=0.0; max_dd=0.0
    for p in rows:
        cumulative+=float(p.get('realized_pnl_usdt') or 0)
        peak=max(peak,cumulative)
        max_dd=max(max_dd,peak-cumulative)

    return {
        'timezone':RISK_TIMEZONE,'day':today.isoformat(),
        'closed_today':len(today_rows),'closed_total':len(rows),
        'realized_pnl_today_usdt':round(realized_today,8),
        'cumulative_realized_pnl_usdt':round(cumulative,8),
        'current_realized_drawdown_usdt':round(max(0.0,peak-cumulative),8),
        'max_realized_drawdown_usdt':round(max_dd,8),
        'consecutive_losses':consecutive_losses,
        'last_closed_at':float(rows[-1].get('closed_at') or 0) if rows else None,
    }
