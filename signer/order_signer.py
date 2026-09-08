from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
MAX_QUOTE = float(os.getenv('MAX_QUOTE_USDT', '10'))
PORT = int(os.getenv('PORT', '8080'))


def signing_key() -> bytes:
    return hashlib.sha256(f'tst-executor-v1:{API_SECRET}'.encode()).digest()


def decode_token(token: str):
    try:
        payload, mac = token.rsplit('.', 1)
        expected = hmac.new(signing_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, expected):
            return None
        padded = payload + '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if time.time() > float(data.get('exp') or 0):
            return None
        pair = str(data.get('pair') or '')
        if not pair.endswith('/USDT') or '/' not in pair:
            return None
        stake = float(data.get('stake_usdt') or 0)
        entry = float(data.get('entry') or 0)
        tp = float(data.get('tp') or 0)
        sl = float(data.get('sl') or 0)
        if not (0 < stake <= MAX_QUOTE and entry > 0 and tp > entry and 0 < sl < entry):
            return None
        return data
    except Exception:
        return None


def signed_spec(path: str, method: str, params: dict):
    ordered = [(k, str(v)) for k, v in params.items() if v is not None and v != '']
    ordered.extend([('recvWindow', '5000'), ('timestamp', str(int(time.time() * 1000)))])
    query = urlencode(ordered)
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return {
        'apiKey': API_KEY,
        'path': path,
        'method': method,
        'query': query,
        'signature': signature,
        'expiresMs': int(time.time() * 1000) + 5000,
    }


def symbol_for(sig):
    return sig['pair'].replace('/', '')


def validate_oco(sig, body):
    qty = float(body.get('quantity') or 0)
    tp = float(body.get('tp') or 0)
    sl_trigger = float(body.get('slTrigger') or 0)
    sl_limit = float(body.get('slLimit') or 0)
    entry = float(sig['entry'])
    stake = float(sig['stake_usdt'])
    max_qty = (stake / entry) * 1.35
    if not (0 < qty <= max_qty):
        raise ValueError('OCO quantity outside authorized range')
    if not (tp > sl_trigger > sl_limit > 0):
        raise ValueError('Invalid OCO price ordering')
    if tp > entry * 1.15 or sl_limit < entry * 0.80:
        raise ValueError('OCO prices outside authorized safety envelope')
    return qty, tp, sl_trigger, sl_limit


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        raw = json.dumps(data, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/health':
            return self.send_json(200, {'ok': True, 'service': 'tst-order-signer', 'configured': bool(API_KEY and API_SECRET)})
        if parsed.path != '/validate':
            return self.send_json(404, {'ok': False, 'error': 'not found'})
        token = parse_qs(parsed.query).get('t', [''])[0]
        sig = decode_token(token)
        if not sig:
            return self.send_json(400, {'ok': False, 'error': 'invalid or expired authorization'})
        safe = {k: sig.get(k) for k in ('id','pair','stake_usdt','entry','tp','sl','iat','exp')}
        return self.send_json(200, {'ok': True, 'signal': safe})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != '/sign':
            return self.send_json(404, {'ok': False, 'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            return self.send_json(400, {'ok': False, 'error': 'invalid json'})
        sig = decode_token(str(body.get('t') or ''))
        if not sig:
            return self.send_json(403, {'ok': False, 'error': 'invalid or expired authorization'})
        op = str(body.get('op') or '')
        symbol = symbol_for(sig)
        sid = ''.join(c for c in str(sig['id']) if c.isalnum())[:20]
        try:
            if op == 'account':
                spec = signed_spec('/api/v3/account', 'GET', {'omitZeroBalances': 'true'})
            elif op == 'query_buy':
                spec = signed_spec('/api/v3/order', 'GET', {'symbol': symbol, 'origClientOrderId': f'tstb_{sid}'})
            elif op == 'buy':
                quote = min(float(sig['stake_usdt']), MAX_QUOTE)
                spec = signed_spec('/api/v3/order', 'POST', {
                    'symbol': symbol,
                    'side': 'BUY',
                    'type': 'MARKET',
                    'quoteOrderQty': f'{quote:.2f}',
                    'newClientOrderId': f'tstb_{sid}',
                    'newOrderRespType': 'FULL',
                })
            elif op == 'query_oco':
                spec = signed_spec('/api/v3/orderList', 'GET', {'origClientOrderId': f'tsto_{sid}'})
            elif op == 'oco':
                qty, tp, sl_trigger, sl_limit = validate_oco(sig, body)
                spec = signed_spec('/api/v3/orderList/oco', 'POST', {
                    'symbol': symbol,
                    'side': 'SELL',
                    'quantity': body.get('quantity'),
                    'listClientOrderId': f'tsto_{sid}',
                    'aboveType': 'LIMIT_MAKER',
                    'abovePrice': body.get('tp'),
                    'belowType': 'STOP_LOSS_LIMIT',
                    'belowStopPrice': body.get('slTrigger'),
                    'belowPrice': body.get('slLimit'),
                    'belowTimeInForce': 'GTC',
                    'newOrderRespType': 'RESULT',
                })
            else:
                return self.send_json(400, {'ok': False, 'error': 'unsupported op'})
            return self.send_json(200, {'ok': True, 'spec': spec})
        except Exception as exc:
            return self.send_json(400, {'ok': False, 'error': str(exc)[:300]})

    def log_message(self, *_args):
        return


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
