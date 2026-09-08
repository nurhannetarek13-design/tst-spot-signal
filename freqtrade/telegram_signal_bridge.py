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
CHAT_ID_FILE = Path('/freqtrade/user_data/telegram_chat_id.txt')

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


def get_chat_id() -> str:
    try:
        value = CHAT_ID_FILE.read_text(encoding='utf-8').strip()
        if value:
            return value
    except Exception:
        pass
    return TELEGRAM_CHAT_ID


def tg_api(method: str, payload: dict) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not configured')
    payload = dict(payload)
    if method == 'sendMessage':
        cid = get_chat_id()
        if not cid:
            raise RuntimeError('TELEGRAM_CHAT_ID is not configured')
        payload['chat_id'] = cid
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}'
    body = json.dumps(payload).encode()
    req = Request(url, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'tst-signal-bridge/4.1'})
    try:
        with urlopen(req, timeout=25) as r:
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
        recommended = MIN_STAKE_USDT if balance_usdt >= MIN_STAKE_USDT else max(0.0, balance_usdt)
    recommended = round(recommended, 2)
    est_risk = recommended * risk_pct
    note = f'Risk-based: ~{est_risk:.2f} USDT at SL, {(recommended / balance_usdt * 100):.1f}% of free USDT balance.'
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


def send_prealert(pair: str, last: float, change_24h: float, volume_24h: float, range_1h: float, momentum_15m: float, volume_ratio: float) -> None:
    symbol = pair.replace('/', '')
    text = (
        '👀 PRE-ALERT — Setup forming\n'
        f'Pair: {pair}\nPrice: {last:.8g}\n24h change: {change_24h:+.2f}%\n'
        f'24h volume: {volume_24h/1_000_000:.1f}M USDT\n1h range: {range_1h*100:.2f}%\n'
        f'15m momentum: {momentum_15m*100:+.2f}%\nVolume expansion: {volume_ratio:.2f}x\n\n'
        'دي مراقبة مبكرة فقط — مفيش BUY لسه.'
    )
    tg_api('sendMessage', {
        'text': text,
        'reply_markup': {'inline_keyboard': [[{'text': '📈 Watch on Binance', 'url': f'https://www.binance.com/en/trade/{symbol}?type=spot'}]]},
        'disable_web_page_preview': True,
    })
    print(f'[telegram-prealert] sent for {pair}')


def send_opportunity(pair: str, stake_usdt: float, entry: float, tp: float, sl: float, tag: str = '') -> str:
    try:
        balance = get_free_usdt_balance()
    except Exception:
        balance = None
    recommended, risk_usdt, sizing_note = recommend_stake(balance, stake_usdt, entry, sl)
    signal_id = uuid.uuid4().hex[:12]
    now = time.time()
    sig = Signal(signal_id, pair, recommended, entry, tp, sl, now, now + 15 * 60, tag, balance, risk_usdt, sizing_note)
    save_signal(sig)
    balance_line = f'Free USDT: {balance:.2f}\n' if balance is not None else ''
    risk_line = f'Estimated risk at SL: {risk_usdt:.2f} USDT\n' if risk_usdt is not None else ''
    text = (
        '🚨 CONFIRMED BUY — Spot\n'
        f'Pair: {pair}\nEntry ≈ {entry:.8g}\n{balance_line}✅ Amount: {recommended:.2f} USDT\n{risk_line}'
        f'TP: {tp:.8g} (+{((tp/entry)-1)*100:.2f}%)\nSL Trigger: {sl:.8g} (-{(1-(sl/entry))*100:.2f}%)\n'
        f'Expected hold: 30–60 min\nStrategy: {tag or "NFIProtectedX7"}\n\n'
        'اضغطي PREPARE ORDER — هتلاقي كل خانات Binance جاهزة للنسخ قبل فتح Binance.'
    )
    if not PUBLIC_BASE_URL:
        raise RuntimeError('SIGNAL_PUBLIC_BASE_URL is not configured')
    buttons = [[{'text': '✅ PREPARE ORDER', 'url': f'{PUBLIC_BASE_URL}/buy?id={signal_id}'}]]
    tg_api('sendMessage', {'text': text, 'reply_markup': {'inline_keyboard': buttons}, 'disable_web_page_preview': True})
    print(f'[telegram-buy] sent for {pair} id={signal_id}')
    return signal_id


def order_preview(sig: Signal) -> str:
    pair = html.escape(sig.pair)
    symbol = html.escape(sig.pair.replace('/', ''))
    strategy = html.escape(sig.tag or 'NFIProtectedX7')
    entry = f'{sig.entry:.8g}'
    tp = f'{sig.tp:.8g}'
    sl_trigger_value = sig.sl
    sl_limit_value = sig.sl * 0.9985
    sl_trigger = f'{sl_trigger_value:.8g}'
    sl_limit = f'{sl_limit_value:.8g}'
    stake = f'{sig.stake_usdt:.2f}'
    qty = sig.stake_usdt / sig.entry if sig.entry > 0 else 0
    qty_text = f'{qty:.8g}'
    profit_usdt = qty * max(sig.tp - sig.entry, 0)
    loss_usdt = qty * max(sig.entry - sl_limit_value, 0)
    reward_pct = ((sig.tp / sig.entry) - 1) * 100 if sig.entry else 0
    trigger_risk_pct = (1 - (sl_trigger_value / sig.entry)) * 100 if sig.entry else 0
    limit_risk_pct = (1 - (sl_limit_value / sig.entry)) * 100 if sig.entry else 0
    seconds = max(0, int(sig.expires_at - time.time()))
    note = html.escape(sig.sizing_note or '')
    rows = [
        ('Price', entry, ''),
        ('Total', stake, ' USDT'),
        ('Amount', qty_text, f' {html.escape(sig.pair.split("/")[0])}'),
        ('TP Limit', tp, f'  (+{reward_pct:.2f}%)'),
        ('SL Trigger', sl_trigger, f'  (-{trigger_risk_pct:.2f}%)'),
        ('SL Price', sl_limit, f'  (-{limit_risk_pct:.2f}%)'),
    ]
    row_html = ''.join(
        f'<div class="field"><div><div class="label">{label}</div><div class="val">{value}<span>{suffix}</span></div></div>'
        f'<button type="button" onclick="copyVal(this, \'{value}\')">COPY</button></div>'
        for label, value, suffix in rows
    )
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{pair} Order Prep</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#0b0e11;color:#eaecef;font-family:Arial,sans-serif;padding:16px}}.card{{max-width:520px;margin:10px auto;background:#181a20;border:1px solid #2b3139;border-radius:18px;padding:20px}}h1{{font-size:25px;margin:5px 0}}.sub{{color:#848e9c;margin-bottom:18px;font-size:13px;overflow-wrap:anywhere}}.badge{{display:inline-block;padding:6px 10px;background:#2b3139;border-radius:8px;font-size:12px}}.field{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 0;border-bottom:1px solid #2b3139}}.label{{font-size:13px;color:#848e9c;margin-bottom:4px}}.val{{font-size:18px;font-weight:800}}.val span{{font-size:12px;font-weight:500;color:#848e9c}}button{{border:0;background:#2b3139;color:#fcd535;padding:9px 12px;border-radius:8px;font-weight:800}}button.done{{background:#0ecb81;color:#0b0e11}}.tip{{background:#202630;padding:12px;border-radius:10px;font-size:13px;line-height:1.5;margin:16px 0}}.note{{font-size:12px;color:#848e9c;line-height:1.5}}a.btn{{display:block;text-align:center;text-decoration:none;background:#fcd535;color:#181a20;font-weight:900;padding:15px;border-radius:10px;margin-top:16px}}.expires{{font-size:13px;color:#848e9c;text-align:center;margin-top:12px}}</style><script>async function copyVal(btn,v){{try{{await navigator.clipboard.writeText(v);btn.textContent='COPIED';btn.classList.add('done');setTimeout(()=>{{btn.textContent='COPY';btn.classList.remove('done')}},1200)}}catch(e){{window.prompt('Copy this value:',v)}}}}</script></head><body><div class="card"><div class="badge">BINANCE SPOT · ORDER PREP</div><h1>{pair}</h1><div class="sub">{strategy}</div>{row_html}<div class="tip"><b>في Binance:</b><br>Price ← Price<br>Total ← Total<br>TP Limit ← TP Limit<br>SL Trigger ← SL Trigger<br>SL Price ← SL Price<br><br>SL Price متحطوط أقل من Trigger بحوالي 0.15% عشان يزيد احتمال تنفيذ وقف الخسارة بعد التفعيل.</div><div class="note">Estimated profit at TP: +{profit_usdt:.4f} USDT · Estimated loss if SL Limit fills: -{loss_usdt:.4f} USDT.<br>{note}</div><a class="btn" href="https://www.binance.com/en/trade/{symbol}?type=spot">OPEN {pair} ON BINANCE</a><div class="expires">Signal expires in {seconds // 60}:{seconds % 60:02d}</div></div></body></html>'''


def resolve_chat_loop() -> None:
    while True:
        try:
            me = tg_api('getMe', {})
            bot_id = str((me.get('result') or {}).get('id') or '')
            current = get_chat_id()
            if current and current != bot_id:
                return
            updates = tg_api('getUpdates', {'limit':100, 'timeout':20, 'allowed_updates':['message']})
            found = None
            for upd in updates.get('result') or []:
                msg = upd.get('message') or {}; chat = msg.get('chat') or {}; sender = msg.get('from') or {}
                if chat.get('type') == 'private' and chat.get('id') is not None and not sender.get('is_bot'):
                    found = str(chat['id'])
            if found:
                CHAT_ID_FILE.write_text(found, encoding='utf-8')
                print('[telegram-resolve] private chat connected successfully')
                try:
                    tg_api('sendMessage', {'text': '✅ Telegram connected to TST Signal Bot'})
                except Exception as exc:
                    print(f'[telegram-resolve] confirmation send failed: {exc}')
                return
        except Exception as exc:
            print(f'[telegram-resolve] waiting for private /start: {type(exc).__name__}: {exc}')
        time.sleep(3)


def watch_new_listings() -> None:
    known: set[str] | None = None
    while True:
        try:
            info = binance_get('/exchangeInfo')
            markets = {s['symbol']: s for s in info.get('symbols', []) if s.get('quoteAsset') == 'USDT' and s.get('isSpotTradingAllowed', True)}
            current = set(markets)
            if known is None:
                known = current
            else:
                for sym in sorted(current - known):
                    s = markets[sym]
                    pair = f"{s.get('baseAsset')}/USDT"
                    status = s.get('status', 'UNKNOWN')
                    tg_api('sendMessage', {'text': f'🆕 NEW LISTING WATCH\nPair: {pair}\nStatus: {status}\nالبوت ضافها للسكان تلقائيًا.', 'disable_web_page_preview': True})
                    print(f'[new-listing-watch] sent {pair}')
                known = current
        except Exception as e:
            print(f'[new-listing-watch] warning: {type(e).__name__}: {e}')
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, text: str, content_type: str = 'text/plain; charset=utf-8'):
        body = text.encode(); self.send_response(status); self.send_header('Content-Type', content_type); self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == '/health': return self._send(200, 'ok')
        if parsed.path != '/buy': return self._send(404, 'not found')
        sid = parse_qs(parsed.query).get('id', [''])[0]; sig = load_signal(sid)
        if not sig: return self._send(404, 'signal not found')
        if time.time() > sig.expires_at: return self._send(410, 'signal expired')
        return self._send(200, order_preview(sig), 'text/html; charset=utf-8')
    def log_message(self, *_args): return


def run_http():
    port = int(os.getenv('PORT', '8080')); HTTPServer(('0.0.0.0', port), Handler).serve_forever()


def announce_online() -> None:
    try:
        tg_api('sendMessage', {'text': '✅ TST Signal Bot ONLINE\nLive Binance data · Signal-only · No auto-buy\n🚨 BUY → PREPARE ORDER → copy Price/TP/SL → Binance', 'disable_web_page_preview': True})
        print('[telegram] ONLINE message sent successfully')
    except Exception as exc:
        print(f'[telegram] ONLINE pending until private /start: {type(exc).__name__}: {exc}')


if __name__ == '__main__':
    threading.Thread(target=resolve_chat_loop, daemon=True).start()
    announce_online()
    threading.Thread(target=watch_new_listings, daemon=True).start()
    threading.Thread(target=run_http, daemon=False).start()
