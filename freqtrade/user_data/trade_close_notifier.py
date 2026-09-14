from __future__ import annotations

import os
import sys
import time

# This file runs from /freqtrade/user_data, so make the app root importable.
if '/freqtrade' not in sys.path:
    sys.path.insert(0, '/freqtrade')

import telegram_signal_bridge as bridge
import trade_state

POLL_SEC = max(10, int(os.getenv('TRADE_CLOSE_NOTIFY_POLL_SEC', '15')))
STARTED_AT = time.time()
BACKFILL_GRACE_SEC = max(30, int(os.getenv('TRADE_CLOSE_NOTIFY_BACKFILL_GRACE_SEC', '120')))

_REASON_LABELS = {
    'TAKE_PROFIT': 'TP 🎯',
    'STOP_LOSS': 'SL 🛑',
    'TIME_EXIT': 'TIME EXIT ⏰',
    'BINANCE_OCO_EXIT': 'OCO EXIT',
    'PROFIT_LOCK': 'PROFIT LOCK 🔒',
    'REVERSAL': 'REVERSAL ↩️',
}


def _send(text: str) -> None:
    bridge.tg_api(
        'sendMessage',
        {'text': text, 'disable_web_page_preview': True},
    )


def _notify(pos: dict) -> bool:
    signal_id = str(pos.get('signal_id') or '')
    symbol = str(pos.get('symbol') or '')
    if not signal_id or not symbol or pos.get('close_notified_at'):
        return False

    closed_at = float(pos.get('closed_at') or 0.0)
    # Do not dump historical closes into Telegram when this notifier is first
    # introduced or after a long restart. Fresh closes around a restart are kept.
    if closed_at > 0 and closed_at < STARTED_AT - BACKFILL_GRACE_SEC:
        trade_state.update_position(
            signal_id,
            close_notified_at=time.time(),
            close_notify_backfill_suppressed=True,
        )
        return False

    entry = float(pos.get('entry') or 0.0)
    exit_price = float(pos.get('exit_price') or 0.0)
    quote_spent = float(pos.get('quote_spent') or 0.0)
    exit_quote = float(pos.get('exit_quote') or 0.0)
    pnl = float(pos.get('realized_pnl_usdt') or 0.0)
    pnl_pct = (pnl / quote_spent * 100.0) if quote_spent > 0 else 0.0
    opened_at = float(pos.get('opened_at') or 0.0)
    closed_at = closed_at or time.time()
    held_min = max(0, int(round((closed_at - opened_at) / 60.0))) if opened_at > 0 else 0
    reason = str(pos.get('close_reason') or 'CLOSED').upper()
    reason_label = _REASON_LABELS.get(reason, reason.replace('_', ' '))
    result_emoji = '✅' if pnl >= 0 else '❌'

    text = (
        f'{result_emoji} TRADE CLOSED — {symbol} — SPOT\n'
        f'💵 Stake {quote_spent:.2f} USDT\n'
        f'💲 Entry {entry:.8g}\n'
        f'🏁 Exit {exit_price:.8g}\n'
        f'📊 Result {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\n'
        f'📌 Reason {reason_label}\n'
        f'⏱ Held {held_min} min\n'
        f'💰 Exit value {exit_quote:.2f} USDT'
    )
    _send(text)
    trade_state.update_position(signal_id, close_notified_at=time.time())
    trade_state.append_event(
        'TRADE_CLOSE_TELEGRAM_SENT',
        signal_id=signal_id,
        symbol=symbol,
        realized_pnl_usdt=pnl,
        pnl_pct=pnl_pct,
        close_reason=reason,
    )
    print(
        f'[trade-close-notifier] SENT {symbol} pnl={pnl:+.4f}USDT reason={reason}',
        flush=True,
    )
    return True


def run_once() -> None:
    for pos in trade_state.closed_positions():
        try:
            _notify(pos)
        except Exception as exc:
            print(
                f"[trade-close-notifier] {pos.get('symbol')} warning {type(exc).__name__}: {str(exc)[:180]}",
                flush=True,
            )


def main() -> None:
    print(
        f'[trade-close-notifier] ONLINE poll={POLL_SEC}s backfill_grace={BACKFILL_GRACE_SEC}s',
        flush=True,
    )
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'[trade-close-notifier] loop warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
