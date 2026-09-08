from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SIGNAL_DIR = Path('/freqtrade/user_data/signals')
SIGNAL_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
PUBLIC_BASE_URL = os.getenv('SIGNAL_PUBLIC_BASE_URL', '').rstrip('/')
BINANCE_PUBLIC = 'https://data-api.binance.vision/api/v3'
BINANCE_PRIVATE = 'https://api.binance.com/api/v3'
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
DAILY_LOSS_LIMIT_USDT = float(os.getenv('DAILY_LOSS_LIMIT_USDT', '2.0'))
MAX_RISK_PER_TRADE_USDT = float(os.getenv('MAX_RISK_PER_TRADE_USDT', '0.75'))
RISK_FRACTION_OF_BALANCE = float(os.getenv('RISK_FRACTION_OF_BALANCE', '0.02'))
MAX_STAKE_FRACTION = float(os.getenv('MAX_STAKE_FRACTION', '0.40'))
MIN_STAKE_USDT = float(os.getenv('MIN_STAKE_USDT', '5.5'))


def tg_api(method: str, payload: dict) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not configured')
    if not TELEGRAM_CHAT_ID and method == 'sendMessage':
        raise RuntimeError('TELEGRAM_CHAT_ID is not configured')
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}'
    body = json.dumps(payload).encode()
    req = Request(url, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'tst-signal-bridge/3.0'})
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except HTTPError as exc:
        try:
            detail = exc.read().decode(errors='replace')[:500]
        except Exception:
            detail = ''
        raise RuntimeError(f'Telegram HTTP {exc.code}: {detail}') from exc


def binance_get(path: str) -> dict:
    req = Request(BINANCE_PUBLIC + path, headers={'User-Agent': 'tst-new-listing-watch/2.0'})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get_free_usdt_balance() -> float | None:
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return None
    params = {'timestamp': int(time.time() * 1000), 'recvWindow': 5000}
    query = urlencode(params)
    signature = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    req = Request(
        f'{BINANCE_PRIVATE}/account?{query}&signature={signature}',
        headers={'X-MBX-APIKEY': BINANCE_API_KEY, 'User-Agent': 'tst-balance-reader/1.0'},
    )
    with urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    for item in data.get('balances', []):
        if item.get('asset') == 'USDT':
            return float(item.get('free') or 0.0)
    return 0.0


def recommend_stake(balance_usdt: float | None, fallback_stake: float, entry: float, sl: float) -> tuple[float, float | None, str]:
    risk_pct = abs(entry - sl) / entry if entry > 0 else 0.0
    if balance_usdt is None or balance_usdt <= 0 or risk_pct <= 0:
        return round(max(MIN_STAKE_USDT, fallback_stake), 2), None, 'Fallback sizing — live USDT balance unavailable.'

    risk_budget = min(MAX_RISK_PER_TRADE_USDT, balance_usdt * RISK_FRACTION_OF_BALANCE, DAILY_LOSS_LIMIT_USDT / 2.0)
    by_risk = risk_budget / risk_pct
    by_balance = balance_usdt * MAX_STAKE_FRACTION
    reserve_cap = max(0.0, balance_usdt - min(2.0, balance_usdt * 0.10))
    recommended = min(by_risk, by_balance, reserve_cap)

    if recommended < MIN_STAKE_USDT:
        if balance_usdt >= MIN_STAKE_USDT:
            recommended = MIN_STAKE_USDT
        else:
            recommended = max(0.0, balance_usdt)

    recommended = round(recommended, 2)
    est_risk = recommended * risk_pct
    note = (
        f'Risk-based: ~{est_risk:.2f} USDT at SL, '
        f'{(recommended / balance_usdt * 100):.1f}% of free USDT balance.'
    )
    return recommended, round(est_risk, 4), note


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
    balance_usdt: float | None = None
    risk_usdt: float | None = None
    sizing_note: str = ''


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


def send_prealert(
    pair: str,
    last: float,
    change_24h: float,
    volume_24h: float,
    range_1h: float,
    momentum_15m: float,
    volume_ratio: float,
) -> None:
    symbol = pair.replace('/', '')
    text = (
        '👀 PRE-ALERT — Setup forming\n'
        f'Pair: {pair}\n'
        f'Price: {last:.8g}\n'
        f'24h change: {change_24h:+.2f}%\n'
        f'24h volume: {volume_24h/1_000_000:.1f}M USDT\n'
        f'1h range: {range_1h*100:.2f}%\n'
        f'15m momentum: {momentum_15m*100:+.2f}%\n'
        f'Volume expansion: {volume_ratio:.2f}x\n\n'
        'دي مراقبة مبكرة فقط — مفيش BUY لسه. BUY مش هيظهر إلا لو NFI أكد الدخول.'
    )
    tg_api('sendMessage', {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'reply_markup': {'inline_keyboard': [[{
            'text': '📈 Watch on Binance',
            'url': f'https://www.binance.com/en/trade/{symbol}?type=spot',
        }]]},
        'disable_web_page_preview': True,
    })
    print(f'[telegram-prealert] sent for {pair}')


def send_opportunity(pair: str, stake_usdt: float, entry: float, tp: float, sl: float, tag: str = '') -> str:
    try:
        balance = get_free_usdt_balance()
    except Exception as exc:
        print(f'[balance] read failed: {type(exc).__name__}: {exc}')
        balance = None

    recommended, risk_usdt, sizing_note = recommend_stake(balance, stake_usdt, entry, sl)
    signal_id = uuid.uuid4().hex[:12]
    now = time.time()
    sig = Signal(signal_id, pair, recommended, entry, tp, sl, now, now + 15 * 60, tag, balance, risk_usdt, sizing_note)
    save_signal(sig)
    symbol = pair.replace('/', '')
    balance_line = f'Free USDT: {balance:.2f}\n' if balance is not None else 'Free USDT: unavailable (fallback sizing)\n'
    risk_line = f'Estimated risk at SL: {risk_usdt:.2f} USDT\n' if risk_usdt is not None else ''
    text = (
        '🚨 NFI CONFIRMED BUY — Spot\n'
        f'Pair: {pair}\n'
        f'Entry ≈ {entry:.8g}\n'
        f'{balance_line}'
        f'✅ Recommended amount: {recommended:.2f} USDT\n'
        f'{risk_line}'
        f'TP: {tp:.8g} (+{((tp/entry)-1)*100:.2f}%)\n'
        f'SL: {sl:.8g} (-{(1-(sl/entry))*100:.2f}%)\n'
        'Window: 15 min\n'
        'Expected hold: 30–60 min\n'
        f'Strategy: {tag or "NFIProtectedX7"}\n'
        f'Sizing: {sizing_note}\n\n'
        'البوت لا يشتري تلقائيًا. اضغطي BUY لفتح صفحة الصفقة جاهزة بكل البيانات.'
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
    print(f'[telegram-buy] sent for {pair} id={signal_id}')
    return signal_id


def order_preview(sig: Signal) -> str:
    pair = html.escape(sig.pair)
    symbol = html.escape(sig.pair.replace('/', ''))
    strategy = html.escape(sig.tag or 'NFIProtectedX7')
    entry, tp, sl = f'{sig.entry:.8g}', f'{sig.tp:.8g}', f'{sig.sl:.8g}'
    stake = f'{sig.stake_usdt:.2f}'
    qty = sig.stake_usdt / sig.entry if sig.entry > 0 else 0
    qty_text = f'{qty:.8g}'
    profit_usdt = qty * max(sig.tp - sig.entry, 0)
    loss_usdt = qty * max(sig.entry - sig.sl, 0)
    reward_pct = ((sig.tp / sig.entry) - 1) * 100 if sig.entry else 0
    risk_pct = (1 - (sig.sl / sig.entry)) * 100 if sig.entry else 0
    seconds = max(0, int(sig.expires_at - time.time()))
    balance_row = '' if sig.balance_usdt is None else f'<div class="row"><span class="label">Free USDT balance</span><span class="value">{sig.balance_usdt:.2f} USDT</span></div>'
    risk_row = '' if sig.risk_usdt is None else f'<div class="row"><span class="label">Estimated risk at SL</span><span class="value red">-{sig.risk_usdt:.4f} USDT</span></div>'
    note = html.escape(sig.sizing_note or '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{pair} Order Preview</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#0b0e11;color:#eaecef;font-family:Arial,sans-serif;padding:20px}}.card{{max-width:520px;margin:20px auto;background:#181a20;border:1px solid #2b3139;border-radius:18px;padding:22px}}h1{{font-size:24px;margin:0 0 6px}}.sub{{color:#848e9c;margin-bottom:20px}}.row{{display:flex;justify-content:space-between;gap:20px;padding:12px 0;border-bottom:1px solid #2b3139}}.label{{color:#848e9c}}.value{{font-weight:700;text-align:right}}.green{{color:#0ecb81}}.red{{color:#f6465d}}.note{{font-size:13px;color:#848e9c;line-height:1.5;margin:18px 0}}a.btn{{display:block;text-align:center;text-decoration:none;background:#fcd535;color:#181a20;font-weight:800;padding:15px;border-radius:10px;margin-top:16px}}.badge{{display:inline-block;padding:5px 9px;background:#2b3139;border-radius:8px;font-size:12px;margin-bottom:12px}}</style></head><body><div class="card"><div class="badge">SPOT · MANUAL CONFIRMATION</div><h1>{pair}</h1><div class="sub">{strategy}</div>{balance_row}<div class="row"><span class="label">Recommended amount</span><span class="value">{stake} USDT</span></div><div class="row"><span class="label">Estimated quantity</span><span class="value">{qty_text}</span></div><div class="row"><span class="label">Entry</span><span class="value">{entry}</span></div><div class="row"><span class="label">Take Profit</span><span class="value green">{tp} (+{reward_pct:.2f}%)</span></div><div class="row"><span class="label">Stop Loss</span><span class="value red">{sl} (-{risk_pct:.2f}%)</span></div><div class="row"><span class="label">Estimated TP profit</span><span class="value green">+{profit_usdt:.4f} USDT</span></div><div class="row"><span class="label">Estimated SL loss</span><span class="value red">-{loss_usdt:.4f} USDT</span></div>{risk_row}<div class="row"><span class="label">Signal expires in</span><span class="value">{seconds // 60}:{seconds % 60:02d}</span></div><p class="note">{note}</p><p class="note">كل بيانات الصفقة معروضة هنا تلقائيًا من الإشارة. الصفحة لا تنفذ أي شراء ولا تحتاج كتابة الأرقام يدويًا.</p><a class="btn" href="https://www.binance.com/en/trade/{symbol}?type=spot">OPEN {pair} ON BINANCE</a></div></body></html>'''


def watch_new_listings() -> None:
    known: set[str] | None = None
    while True:
        try:
            info = binance_get('/exchangeInfo')
            markets = {
                s['symbol']: s for s in info.get('symbols', [])
                if s.get('quoteAsset') == 'USDT' and s.get('isSpotTradingAllowed', True)
            }
            current = set(markets)
            if known is None:
                known = current
            else:
                for sym in sorted(current - known):
                    s = markets[sym]
                    pair = f"{s.get('baseAsset')}/USDT"
                    status = s.get('status', 'UNKNOWN')
                    text = (
                        '🆕 NEW LISTING WATCH\n'
                        f'Pair: {pair}\n'
                        f'Status: {status}\n'
                        'البوت ضافها للسكان تلقائيًا. الإشارة لن تتبعت غير بعد ما يبقى فيه بيانات كفاية وNFI يوافق على الدخول.'
                    )
                    tg_api('sendMessage', {
                        'chat_id': TELEGRAM_CHAT_ID,
                        'text': text,
                        'reply_markup': {'inline_keyboard': [[{
                            'text': '📈 Open Binance',
                            'url': f'https://www.binance.com/en/trade/{sym}?type=spot'
                        }]]},
                        'disable_web_page_preview': True,
                    })
                    print(f'[new-listing-watch] sent {pair}')
                known = current
        except Exception as e:
            print(f'[new-listing-watch] warning: {type(e).__name__}: {e}')
        time.sleep(60)


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


def announce_online() -> None:
    try:
        tg_api('sendMessage', {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': (
                '✅ TST Signal Bot ONLINE\n'
                'Live Binance market data · Signal-only · No auto-buy\n'
                '👀 PRE-ALERT = setup forming\n'
                '🚨 BUY = NFI confirmed'
            ),
            'disable_web_page_preview': True,
        })
        print('[telegram] ONLINE message sent successfully')
    except Exception as exc:
        print(f'[telegram] ONLINE message FAILED: {type(exc).__name__}: {exc}')


if __name__ == '__main__':
    announce_online()
    threading.Thread(target=watch_new_listings, daemon=True).start()
    threading.Thread(target=run_http, daemon=False).start()
