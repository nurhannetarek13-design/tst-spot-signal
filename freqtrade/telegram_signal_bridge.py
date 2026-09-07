from __future__ import annotations

import html
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
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
        f'البوت لن يشتري تلقائيًا. اضغطي BUY لفتح صفحة الصفقة جاهزة بكل البيانات.'
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


def order_preview(sig: Signal) -> str:
    pair = html.escape(sig.pair)
    symbol = html.escape(sig.pair.replace('/', ''))
    strategy = html.escape(sig.tag or 'NFIProtectedX7')
    entry = f'{sig.entry:.8g}'
    tp = f'{sig.tp:.8g}'
    sl = f'{sig.sl:.8g}'
    stake = f'{sig.stake_usdt:.2f}'
    qty = sig.stake_usdt / sig.entry if sig.entry > 0 else 0
    qty_text = f'{qty:.8g}'
    profit_usdt = qty * max(sig.tp - sig.entry, 0)
    loss_usdt = qty * max(sig.entry - sig.sl, 0)
    reward_pct = ((sig.tp / sig.entry) - 1) * 100 if sig.entry else 0
    risk_pct = (1 - (sig.sl / sig.entry)) * 100 if sig.entry else 0
    seconds = max(0, int(sig.expires_at - time.time()))

    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{pair} Order Preview</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#0b0e11;color:#eaecef;font-family:Arial,sans-serif;padding:20px}}
.card{{max-width:520px;margin:20px auto;background:#181a20;border:1px solid #2b3139;border-radius:18px;padding:22px}}
h1{{font-size:24px;margin:0 0 6px}}.sub{{color:#848e9c;margin-bottom:20px}}.row{{display:flex;justify-content:space-between;gap:20px;padding:12px 0;border-bottom:1px solid #2b3139}}.label{{color:#848e9c}}.value{{font-weight:700;text-align:right}}.green{{color:#0ecb81}}.red{{color:#f6465d}}.note{{font-size:13px;color:#848e9c;line-height:1.5;margin:18px 0}}a.btn{{display:block;text-align:center;text-decoration:none;background:#fcd535;color:#181a20;font-weight:800;padding:15px;border-radius:10px;margin-top:16px}}.badge{{display:inline-block;padding:5px 9px;background:#2b3139;border-radius:8px;font-size:12px;margin-bottom:12px}}
</style></head><body><div class="card">
<div class="badge">SPOT · MANUAL CONFIRMATION</div><h1>{pair}</h1><div class="sub">{strategy}</div>
<div class="row"><span class="label">Amount</span><span class="value">{stake} USDT</span></div>
<div class="row"><span class="label">Estimated quantity</span><span class="value">{qty_text}</span></div>
<div class="row"><span class="label">Entry</span><span class="value">{entry}</span></div>
<div class="row"><span class="label">Take Profit</span><span class="value green">{tp} (+{reward_pct:.2f}%)</span></div>
<div class="row"><span class="label">Stop Loss</span><span class="value red">{sl} (-{risk_pct:.2f}%)</span></div>
<div class="row"><span class="label">Estimated TP profit</span><span class="value green">+{profit_usdt:.4f} USDT</span></div>
<div class="row"><span class="label">Estimated SL loss</span><span class="value red">-{loss_usdt:.4f} USDT</span></div>
<div class="row"><span class="label">Signal expires in</span><span class="value">{seconds // 60}:{seconds % 60:02d}</span></div>
<p class="note">كل بيانات الصفقة معروضة هنا تلقائيًا من الإشارة. الصفحة لا تنفذ أي شراء ولا تحتاج كتابة الأرقام يدويًا. افتحي Binance من الزر بالأسفل لإتمام العملية بنفسك.</p>
<a class="btn" href="https://www.binance.com/en/trade/{symbol}?type=spot">OPEN {pair} ON BINANCE</a>
</div></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, text: str, content_type: str = 'text/plain; charset=utf-8'):
        body = text.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
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
        return self._send(200, order_preview(sig), 'text/html; charset=utf-8')

    def log_message(self, *_args):
        return


def run_http():
    port = int(os.getenv('PORT', '8080'))
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()


if __name__ == '__main__':
    threading.Thread(target=run_http, daemon=False).start()
