from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import telegram_signal_bridge as legacy

CHAT_ID_FILE = legacy.CHAT_ID_FILE
get_chat_id = legacy.get_chat_id
tg_api = legacy.tg_api

BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
PUBLIC_DOMAIN = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()
EXECUTOR_URL = os.getenv('EXECUTOR_PUBLIC_URL', f'https://{PUBLIC_DOMAIN}/execute' if PUBLIC_DOMAIN else '').strip()
MANUAL_BASE_URL = os.getenv('SIGNAL_PUBLIC_BASE_URL', '').rstrip('/')


def _executor_key() -> bytes:
    if not BINANCE_API_SECRET:
        raise RuntimeError('BINANCE_API_SECRET is required for one-tap executor link signing')
    return hashlib.sha256(f'tst-executor-v1:{BINANCE_API_SECRET}'.encode()).digest()


def _executor_token(signal_id: str, pair: str, stake: float, entry: float, tp: float, sl: float, ttl_sec: int = 15 * 60) -> str:
    payload_obj = {
        'id': signal_id,
        'pair': pair,
        'stake_usdt': round(float(stake), 2),
        'entry': float(entry),
        'tp': float(tp),
        'sl': float(sl),
        'iat': int(time.time()),
        'exp': int(time.time()) + ttl_sec,
    }
    raw = json.dumps(payload_obj, separators=(',', ':'), ensure_ascii=False).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    mac = hmac.new(_executor_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{payload}.{mac}'


def send_prealert(**_kwargs) -> None:
    return


def send_opportunity(pair: str, stake_usdt: float, entry: float, tp: float, sl: float, tag: str = '') -> str:
    try:
        balance = legacy.get_free_usdt_balance()
    except Exception:
        balance = None
    recommended, risk_usdt, sizing_note = legacy.recommend_stake(balance, stake_usdt, entry, sl)
    signal_id = uuid.uuid4().hex[:12]

    manual_url = None
    try:
        now = time.time()
        sig = legacy.Signal(signal_id, pair, recommended, entry, tp, sl, now, now + 15 * 60, tag, balance, risk_usdt, sizing_note)
        legacy.save_signal(sig)
        if MANUAL_BASE_URL and hasattr(legacy, 'make_order_token'):
            manual_url = f'{MANUAL_BASE_URL}/buy?t={legacy.make_order_token(sig)}'
    except Exception:
        manual_url = None

    if not EXECUTOR_URL:
        raise RuntimeError('EXECUTOR_PUBLIC_URL/RAILWAY_PUBLIC_DOMAIN missing')
    token = _executor_token(signal_id, pair, recommended, entry, tp, sl)
    confirm_url = f'{EXECUTOR_URL}?t={token}'
    risk_line = f'Estimated risk at SL: {risk_usdt:.2f} USDT\n' if risk_usdt is not None else ''
    text = (
        '🚨 CONFIRMED BUY — Binance Spot\n'
        f'Pair: {pair}\n'
        f'Entry reference: {entry:.8g}\n'
        f'✅ Spend: {recommended:.2f} USDT\n'
        f'{risk_line}'
        f'TP plan: {tp:.8g} (+{((tp/entry)-1)*100:.2f}%)\n'
        f'SL plan: {sl:.8g} (-{(1-(sl/entry))*100:.2f}%)\n'
        f'Expected hold: 30–60 min\n'
        f'Strategy: {tag or "FAST"}\n\n'
        '⚡ اضغطي ONE-TAP CONFIRM، راجعي الصفقة، وبعدها CONFIRM BUY. فتح الرابط وحده لا ينفذ شراء.'
    )
    rows = [[{'text': '⚡ ONE-TAP CONFIRM', 'url': confirm_url}]]
    if manual_url:
        rows.append([{'text': '📋 Manual Backup', 'url': manual_url}])
    legacy.tg_api('sendMessage', {
        'text': text,
        'reply_markup': {'inline_keyboard': rows},
        'disable_web_page_preview': True,
    })
    print(f'[telegram-buy] one-tap sent for {pair} id={signal_id}')
    return signal_id
