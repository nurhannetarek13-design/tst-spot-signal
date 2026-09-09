from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

STATE_PATH = Path(os.getenv('TST_TRADE_STATE_PATH', '/tmp/tst_live_positions.json'))
EVENT_PATH = Path(os.getenv('TST_TRADE_EVENT_PATH', '/tmp/tst_trade_events.jsonl'))


def _empty() -> dict[str, Any]:
    return {'version': 1, 'updated_at': time.time(), 'positions': {}}


def load_state() -> dict[str, Any]:
    try:
        row = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if isinstance(row, dict) and isinstance(row.get('positions'), dict):
            return row
    except Exception:
        pass
    return _empty()


def save_state(state: dict[str, Any]) -> None:
    state['updated_at'] = time.time()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=STATE_PATH.name + '.', dir=str(STATE_PATH.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, separators=(',', ':'), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, STATE_PATH)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


def append_event(kind: str, **data: Any) -> None:
    EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {'ts': time.time(), 'kind': kind, **data}
    with EVENT_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n')


def _signal_id(body: dict[str, Any]) -> str:
    return str(body.get('signal_id') or body.get('id') or '').strip()


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
    append_event('BUY_FILLED', signal_id=signal_id, symbol=symbol, entry=entry, quantity=qty, quote_spent=quote)


def record_oco(body: dict[str, Any], response: dict[str, Any]) -> None:
    signal_id = _signal_id(body) or str(response.get('signal_id') or '').strip()
    if not signal_id:
        return
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
    append_event('OCO_ACTIVE', signal_id=signal_id, symbol=symbol, order_list_id=order_list_id, quantity=qty, target=tp, stop=sl)


def update_position(signal_id: str, **changes: Any) -> None:
    state = load_state()
    pos = state['positions'].get(signal_id)
    if not isinstance(pos, dict):
        return
    pos.update(changes)
    pos['updated_at'] = time.time()
    state['positions'][signal_id] = pos
    save_state(state)


def open_positions() -> list[dict[str, Any]]:
    state = load_state()
    return [dict(v) for v in state.get('positions', {}).values() if isinstance(v, dict) and v.get('status') in {'BUY_FILLED', 'OCO_ACTIVE'}]


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
