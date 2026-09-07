from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SIGNAL_DIR = Path('/freqtrade/user_data/signals')
SIGNAL_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
PUBLIC_BASE_URL = os.getenv('SIGNAL_PUBLIC_BASE_URL', '').rstrip('/')


def tg_api(method: str, payload: dict) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not configured')
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}'
    body = json.dumps(payload).encode()
    req = Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


@dataclass
class Signal:
    id: str
    pair: str
    stake_usdt: float
    entry: float
    tp: float
    sl: float
    created_at: float
    expires_at: float
    tag: str


def save_signal(sig: Signal) -> None:
    (SIGNAL_DIR / f'{sig.id}.json').write_text(json.dumps(asdict(sig)), encoding='utf-8')


def load_signal(signal_id: str) -> Signal | None:
    path = SIGNAL_DIR / f'{signal_id}.json'
    if not path.exists():
        return None
    try:
        return Signal(**json.loads(path.read_text(encoding='utf-8')))
    except Exception:
        return None


def send_opportunity(pair: str, stake_usdt: float, entry: float, tp: float, sl: float, tag: str = '') -> str:
    signal_id = uuid.uuid4().hex[:12]
    now = time.time()
    sig = Signal(signal_id, pair, stake_usdt, entry, tp, sl, now, now + 15 * 60, tag)
    save_signal(sig)

    symbol = pair.replace('/', '')
    text = (
        f'🚨 فرصة Spot\n'
        f'Pair: {pair}\n'
        f'Entry ≈ {entry:.8g}\n'
        f'Amount: {stake_usdt:.2f} USDT\n'
        f'TP: {tp:.8g}\n'
        f'SL: {sl:.8g}\n'
        f'Window: 15 min\n'
        f'Strategy: {tag or "NFIProtectedX7"}\n\n'
        f'البوت لن يشتري تلقائيًا. التنفيذ يحتاج ضغطك على BUY.'
    )

    buttons = []
    if PUBLIC_BASE_URL:
        buttons.append({'text': '✅ BUY', 'url': f'{PUBLIC_BASE_URL}/buy?id={signal_id}'})
    buttons.append({'text': '📈 Binance', 'url': f'https://www.binance.com/en/trade/{symbol}?type=spot'})

    tg_api('sendMessage', {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'reply_markup': {'inline_keyboard': [buttons]},
        'disable_web_page_preview': True,
    })
    return signal_id


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, text: str):
        body = text.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == '/health':
            return self._send(200, 'ok')
        if parsed.path != '/buy':
            return self._send(404, 'not found')
        sid = parse_qs(parsed.query).get('id', [''])[0]
        sig = load_signal(sid)
        if not sig:
            return self._send(404, 'signal not found')
        if time.time() > sig.expires_at:
            return self._send(410, 'signal expired')
        # Deliberately confirmation-only here. Execution is handled by the dedicated
        # Telegram BUY executor once Binance API credentials are configured.
        return self._send(200, f'BUY confirmed for {sig.pair} — executor not armed yet.')

    def log_message(self, *_args):
        return


def run_http():
    port = int(os.getenv('PORT', '8080'))
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()


if __name__ == '__main__':
    threading.Thread(target=run_http, daemon=False).start()
