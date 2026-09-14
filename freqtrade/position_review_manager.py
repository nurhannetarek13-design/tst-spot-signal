from __future__ import annotations

"""Telegram-only lifecycle reviews for bot-owned Spot positions.

This worker never opens, closes, cancels, or modifies an order. The dynamic exit
manager remains the only automated post-entry order manager. Reviews are emitted
once at 45 minutes and once at 60 minutes from the persisted canonical trade
state so Railway restarts do not duplicate them.
"""

import json
import os
import time
import urllib.parse
import urllib.request

import trade_state

POLL_SEC = max(10, int(os.getenv('POSITION_REVIEW_POLL_SEC', '20')))
REVIEW_45_SEC = max(300, int(os.getenv('POSITION_REVIEW_45_SEC', str(45 * 60))))
DEADLINE_60_SEC = max(REVIEW_45_SEC + 60, int(os.getenv('POSITION_REVIEW_60_SEC', str(60 * 60))))
TELEGRAM_BOT_TOKEN = (os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
TELEGRAM_CHAT_ID = (os.getenv('TELEGRAM_CHAT_ID') or '').strip()
PUBLIC_BASES = [
    'https://data-api.binance.vision/api/v3',
    'https://api.binance.com/api/v3',
]


def _send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError('telegram-not-configured')
    body = urllib.parse.urlencode({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'disable_web_page_preview': 'true',
    }).encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
        data=body,
        method='POST',
        headers={'User-Agent': 'tst-position-review/1.0'},
    )
    with urllib.request.urlopen(req, timeout=12):
        pass


def _price(symbol: str) -> float:
    q = urllib.parse.urlencode({'symbol': symbol})
    last = None
    for base in PUBLIC_BASES:
        try:
            req = urllib.request.Request(
                f'{base}/ticker/price?{q}',
                headers={'User-Agent': 'tst-position-review/1.0'},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                row = json.loads(r.read() or b'{}')
            px = float(row.get('price') or 0.0)
            if px > 0:
                return px
        except Exception as exc:
            last = exc
    raise RuntimeError(f'price-unavailable:{type(last).__name__ if last else "unknown"}')


def _status_label(pnl_pct: float, peak_pct: float) -> str:
    if pnl_pct >= 0.50:
        return 'قوية 🟢'
    if pnl_pct >= -0.20:
        return 'شبه ثابتة 🟡'
    if peak_pct > 0.40 and pnl_pct < peak_pct * 0.45:
        return 'الربح بيتراجع 🟠'
    return 'ضعفت 🔴'


def _review_text(pos: dict, current: float, age_sec: float, deadline: bool) -> str:
    symbol = str(pos.get('symbol') or '')
    entry = float(pos.get('entry') or 0.0)
    peak = max(float(pos.get('peak_price') or entry), current)
    pnl_pct = ((current / entry) - 1.0) * 100.0 if entry > 0 else 0.0
    peak_pct = ((peak / entry) - 1.0) * 100.0 if entry > 0 else pnl_pct
    age_min = int(age_sec // 60)
    status = _status_label(pnl_pct, peak_pct)
    if deadline:
        header = '⏰ 60m DECISION DEADLINE'
        footer = (
            'الصفقة وصلت نهاية مدة FAST30_60 المستهدفة. '
            'الـOCO والـDynamic Profit Lock لسه شغالين للحماية؛ '
            'دي مراجعة قرار، مش أمر بيع تلقائي.'
        )
    else:
        header = '⏱ 45m POSITION REVIEW'
        footer = (
            'مراجعة مبكرة. الـOCO والـDynamic Profit Lock شغالين؛ '
            'لو الربح بدأ يتاكل، مدير الخروج يرفع الحماية حسب الحركة.'
        )
    return (
        f'{header}\n'
        f'{symbol}\n'
        f'⌛ Age: {age_min} min\n'
        f'💲 Current: {current:.8g}\n'
        f'📍 Entry ref: {entry:.8g}\n'
        f'📈 P/L ref: {pnl_pct:+.2f}%\n'
        f'🏔 Peak P/L: {peak_pct:+.2f}%\n'
        f'{status}\n\n'
        f'{footer}'
    )


def run_once() -> None:
    now = time.time()
    for pos in trade_state.open_positions():
        signal_id = str(pos.get('signal_id') or '')
        symbol = str(pos.get('symbol') or '')
        opened_at = float(pos.get('opened_at') or 0.0)
        if not signal_id or not symbol or opened_at <= 0:
            continue
        age = now - opened_at
        try:
            current = _price(symbol)
        except Exception as exc:
            print(f'[position-review] {symbol} price warning {type(exc).__name__}:{exc}', flush=True)
            continue

        if age >= REVIEW_45_SEC and not pos.get('review_45_sent_at'):
            try:
                _send(_review_text(pos, current, age, False))
                trade_state.update_position(signal_id, review_45_sent_at=time.time())
                trade_state.append_event('POSITION_REVIEW_45_SENT', signal_id=signal_id, symbol=symbol, current=current)
                print(f'[position-review] SENT 45m {symbol}', flush=True)
            except Exception as exc:
                print(f'[position-review] 45m send failed {symbol} {type(exc).__name__}:{exc}', flush=True)

        # Refresh state after the 45m mutation so the 60m marker remains exact.
        latest = trade_state.position_for_signal(signal_id) or pos
        if age >= DEADLINE_60_SEC and not latest.get('deadline_60_sent_at'):
            try:
                _send(_review_text(latest, current, age, True))
                trade_state.update_position(signal_id, deadline_60_sent_at=time.time())
                trade_state.append_event('POSITION_DEADLINE_60_SENT', signal_id=signal_id, symbol=symbol, current=current)
                print(f'[position-review] SENT 60m {symbol}', flush=True)
            except Exception as exc:
                print(f'[position-review] 60m send failed {symbol} {type(exc).__name__}:{exc}', flush=True)


def main() -> None:
    print(f'[position-review] ONLINE review={REVIEW_45_SEC//60}m deadline={DEADLINE_60_SEC//60}m telegram_only=True', flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'[position-review] loop warning {type(exc).__name__}:{exc}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
