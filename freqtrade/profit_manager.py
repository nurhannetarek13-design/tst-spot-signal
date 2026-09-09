from __future__ import annotations

"""Smart profit manager, shadow/read-only mode.

This process reconciles bot-owned OCOs with Binance using the read-only key,
tracks MFE, and calculates conservative stop-ratchet levels. It NEVER cancels,
replaces, buys, or sells. Live OCO replacement stays disabled until an exact-
order ownership-checked execution route is available.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

import trade_state

API_BASE = 'https://api.binance.com'
PUBLIC_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']
POLL_SEC = max(10, int(os.getenv('PROFIT_MANAGER_POLL_SEC', '20')))
MIN_RATCHET_GAP_SEC = max(30, int(os.getenv('PROFIT_MANAGER_MIN_RATCHET_GAP_SEC', '60')))


def _read_creds() -> tuple[str, str]:
    key = (os.getenv('BINANCE_READ_API_KEY') or os.getenv('BINANCE_API_KEY') or '').strip()
    secret = (os.getenv('BINANCE_READ_API_SECRET') or os.getenv('BINANCE_API_SECRET') or '').strip()
    return key, secret


def _signed_get(path: str, params: dict) -> dict:
    key, secret = _read_creds()
    if not key or not secret:
        raise RuntimeError('read-only Binance credentials unavailable')
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    query = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f'{API_BASE}{path}?{query}&signature={sig}',
        headers={'X-MBX-APIKEY': key, 'User-Agent': 'tst-profit-shadow/1.0'},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read() or b'{}')


def _public(path: str, params: dict):
    q = urllib.parse.urlencode(params)
    last = None
    for base in PUBLIC_BASES:
        try:
            req = urllib.request.Request(f'{base}{path}?{q}', headers={'User-Agent': 'tst-profit-shadow/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b'{}')
        except Exception as exc:
            last = exc
    raise RuntimeError(last or 'public API unavailable')


def _market(symbol: str) -> dict:
    book = _public('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0)
    ask = float(book.get('askPrice') or 0)
    if bid <= 0 or ask <= 0:
        raise RuntimeError('bad book')
    rows = _public('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 35})
    now_ms = int(time.time() * 1000)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if len(rows) < 20:
        raise RuntimeError('insufficient candles')
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    closes = [float(x[4]) for x in rows]
    trs = []
    for i in range(1, len(rows)):
        prev = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr = sum(trs[-14:]) / max(1, len(trs[-14:]))
    swing_low = min(lows[-7:])
    return {'bid': bid, 'ask': ask, 'high20': max(highs[-20:]), 'swing_low7': swing_low, 'atr': atr}


def _owned_oco(pos: dict) -> tuple[bool, dict]:
    oid = int(pos.get('oco_order_list_id') or 0)
    if oid <= 0:
        return False, {}
    row = _signed_get('/api/v3/orderList', {'orderListId': oid})
    owned = str(row.get('listClientOrderId') or '').startswith('TSTO-') and str(row.get('symbol') or '') == str(pos.get('symbol') or '')
    return owned, row


def _suggest_stop(pos: dict, market: dict) -> tuple[float | None, str]:
    entry = float(pos.get('entry') or 0)
    stop = float(pos.get('stop') or 0)
    target = float(pos.get('target') or 0)
    peak = max(float(pos.get('peak_price') or entry), float(market['high20']), float(market['bid']))
    if entry <= 0 or stop <= 0 or target <= entry or peak <= entry:
        return None, 'not-ready'
    initial_risk = float(pos.get('initial_risk_per_unit') or max(entry - stop, entry * 0.001))
    r_multiple = (peak - entry) / max(initial_risk, 1e-12)
    target_progress = (peak - entry) / max(target - entry, 1e-12)

    suggested = stop
    reason = 'hold-original-stop'

    # Stage 1: once price has genuinely paid ~1R, stop giving the whole trade back.
    if r_multiple >= 1.0 or target_progress >= 0.45:
        suggested = max(suggested, entry * 1.0015)
        reason = 'stage1-breakeven-plus'

    # Stage 2: when most of the target has traded, lock a meaningful part of R
    # while leaving enough room under structure/ATR for the larger target.
    if r_multiple >= 1.55 or target_progress >= 0.72:
        structure = float(market['swing_low7']) - 0.20 * float(market['atr'])
        r_lock = entry + 0.65 * initial_risk
        suggested = max(suggested, min(structure, float(market['bid']) * 0.996), r_lock)
        reason = 'stage2-structure-lock'

    # Stage 3: near target, do not allow a RENDER-style near-hit to round-trip.
    if r_multiple >= 2.0 or target_progress >= 0.88:
        near_target_lock = entry + 1.10 * initial_risk
        structure = float(market['swing_low7']) - 0.12 * float(market['atr'])
        suggested = max(suggested, min(structure, float(market['bid']) * 0.995), near_target_lock)
        reason = 'stage3-near-target-lock'

    # A stop at/above current executable price is unsafe; keep a small buffer.
    suggested = min(suggested, float(market['bid']) * 0.994)
    if suggested <= stop * 1.001:
        return None, 'no-material-ratchet'
    return suggested, reason


def run_once() -> None:
    for pos in trade_state.open_positions():
        signal_id = str(pos.get('signal_id') or '')
        if not signal_id or pos.get('status') != 'OCO_ACTIVE':
            continue
        try:
            owned, oco = _owned_oco(pos)
        except Exception as exc:
            print(f"[profit-shadow] {pos.get('symbol')} OCO query unavailable: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
            continue
        if not owned:
            print(f"[profit-shadow] {pos.get('symbol')} blocked: OCO ownership mismatch", flush=True)
            continue
        if str(oco.get('listOrderStatus') or '').upper() in {'ALL_DONE', 'REJECT'} or str(oco.get('listStatusType') or '').upper() in {'ALL_DONE', 'RESPONSE'}:
            trade_state.update_position(signal_id, status='CLOSED', closed_at=time.time(), close_reason='OCO_DONE')
            trade_state.append_event('POSITION_CLOSED', signal_id=signal_id, symbol=pos.get('symbol'), reason='OCO_DONE')
            print(f"[profit-shadow] {pos.get('symbol')} reconciled CLOSED", flush=True)
            continue

        market = _market(str(pos.get('symbol')))
        peak = max(float(pos.get('peak_price') or pos.get('entry') or 0), float(market['high20']), float(market['bid']))
        changes = {'peak_price': peak, 'last_bid': float(market['bid']), 'last_market_check': time.time()}
        if not pos.get('initial_risk_per_unit'):
            changes['initial_risk_per_unit'] = max(0.0, float(pos.get('entry') or 0) - float(pos.get('stop') or 0))
        trade_state.update_position(signal_id, **changes)
        pos = {**pos, **changes}
        suggested, reason = _suggest_stop(pos, market)
        if suggested is None:
            continue
        previous = float(pos.get('shadow_suggested_stop') or 0)
        last_suggested_at = float(pos.get('shadow_suggested_at') or 0)
        if suggested <= previous * 1.001 or time.time() - last_suggested_at < MIN_RATCHET_GAP_SEC:
            continue
        trade_state.update_position(signal_id, shadow_suggested_stop=suggested, shadow_suggested_at=time.time(), shadow_reason=reason)
        trade_state.append_event('PROFIT_RATCHET_SHADOW', signal_id=signal_id, symbol=pos.get('symbol'), current_stop=pos.get('stop'), suggested_stop=suggested, bid=market['bid'], peak=peak, reason=reason)
        print(f"[profit-shadow] WOULD_RATCHET {pos.get('symbol')} stop={float(pos.get('stop') or 0):.10g}->{suggested:.10g} bid={market['bid']:.10g} peak={peak:.10g} reason={reason}", flush=True)


def main() -> None:
    print(f'[profit-shadow] ONLINE read_only=True poll={POLL_SEC}s exact_oco_ownership=True live_replacement=False', flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'[profit-shadow] loop warning: {type(exc).__name__}: {str(exc)[:160]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
