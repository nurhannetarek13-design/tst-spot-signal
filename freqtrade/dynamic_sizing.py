from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASES = [
    'https://api.binance.com',
    'https://api-gcp.binance.com',
    'https://api1.binance.com',
    'https://api2.binance.com',
    'https://api3.binance.com',
    'https://api4.binance.com',
]
ACCOUNT_READ_RELAY = os.getenv(
    'BINANCE_ACCOUNT_READ_RELAY_URL',
    'https://tst-spot-signal.nurhanne-tarek13.workers.dev/account-read-relay',
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _clean_credential(value: str) -> str:
    v = (value or '').replace('\r', '').replace('\n', '').strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1].strip()
    return v


def _validate_hmac_secret(secret: str) -> None:
    upper = secret.upper()
    if 'BEGIN PRIVATE KEY' in upper or 'BEGIN RSA PRIVATE KEY' in upper or 'BEGIN OPENSSH PRIVATE KEY' in upper:
        raise RuntimeError('BINANCE_SECRET_IS_ASYMMETRIC_KEY_NOT_HMAC')


def _signed_account_query(secret: str) -> str:
    _validate_hmac_secret(secret)
    params = [('recvWindow', '5000'), ('timestamp', str(int(time.time() * 1000)))]
    query = urlencode(params)
    signature = hmac.new(secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    return f'{query}&signature={signature}'


def _safe_http_error(exc: HTTPError) -> str:
    try:
        raw = exc.read().decode('utf-8', errors='replace')[:800]
        row = json.loads(raw or '{}')
        code = row.get('code')
        msg = str(row.get('msg') or row.get('status') or '').replace('\n', ' ')[:220]
        if code is not None or msg:
            return f'HTTP_{exc.code}:code={code}:msg={msg}'
        return f'HTTP_{exc.code}'
    except Exception:
        return f'HTTP_{getattr(exc, "code", "ERR")}'


def _relay_free_usdt(key: str, secret: str) -> float:
    caller_secret = _clean_credential(os.getenv('TELEGRAM_BOT_TOKEN') or '')
    if not caller_secret:
        raise RuntimeError('SIZING_RELAY_AUTH_MISSING')
    body = json.dumps({'apiKey': key, 'query': _signed_account_query(secret)}, separators=(',', ':')).encode()
    ts = str(int(time.time() * 1000))
    caller_sig = hmac.new(caller_secret.encode(), ts.encode() + b'.' + body, hashlib.sha256).hexdigest()
    req = Request(
        ACCOUNT_READ_RELAY,
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'tst-dynamic-sizing/5.0',
            'X-Sizing-Timestamp': ts,
            'X-Sizing-Signature': caller_sig,
        },
    )
    try:
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except HTTPError as exc:
        raise RuntimeError(f'SIZING_RELAY_HTTP:{_safe_http_error(exc)}') from exc
    if data.get('ok') is not True or data.get('status') != 'ACCOUNT_READ_OK':
        raise RuntimeError(f'SIZING_RELAY_REJECTED:{str(data.get("status") or "UNKNOWN")[:120]}')
    return max(0.0, float(((data.get('usdt') or {}).get('free')) or 0.0))


def free_usdt() -> float:
    key = _clean_credential(os.getenv('BINANCE_API_KEY') or '')
    secret = _clean_credential(os.getenv('BINANCE_API_SECRET') or '')
    if not key or not secret:
        raise RuntimeError('BINANCE_CREDENTIALS_MISSING_FOR_SIZING')
    _validate_hmac_secret(secret)

    last_error = 'unavailable'
    for base in API_BASES:
        query = _signed_account_query(secret)
        req = Request(
            f'{base}/api/v3/account?{query}',
            headers={'X-MBX-APIKEY': key, 'User-Agent': 'tst-dynamic-sizing/5.0'},
        )
        try:
            with urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            for balance in data.get('balances') or []:
                if balance.get('asset') == 'USDT':
                    return max(0.0, float(balance.get('free') or 0.0))
            return 0.0
        except HTTPError as exc:
            last_error = _safe_http_error(exc)
            if any(marker in last_error for marker in ('code=-1022', 'code=-2014', 'code=-2015', 'code=-1021')):
                break
        except Exception as exc:
            last_error = f'{type(exc).__name__}:{str(exc)[:180]}'

    try:
        return _relay_free_usdt(key, secret)
    except Exception as exc:
        raise RuntimeError(f'BINANCE_BALANCE_READ_FAILED:direct={last_error}; relay={type(exc).__name__}:{str(exc)[:260]}') from exc


def recommended_stake(stop_pct: float) -> tuple[float | None, float]:
    free = free_usdt()
    min_stake = max(5.0, _env_float('MIN_STAKE_USDT', 5.0))
    max_stake_fraction = min(max(_env_float('MAX_STAKE_FRACTION', 0.50), 0.01), 0.95)
    risk_fraction = min(max(_env_float('RISK_FRACTION_OF_BALANCE', 0.02), 0.001), 0.20)
    max_risk = max(0.01, _env_float('MAX_RISK_PER_TRADE_USDT', 0.50))

    stop = max(float(stop_pct), 0.001)
    risk_budget = min(max_risk, free * risk_fraction)
    by_risk = risk_budget / stop
    by_balance = free * max_stake_fraction
    available = max(0.0, free - 0.10)
    raw = min(10.0, available, by_balance, by_risk)
    stake = math.floor(raw * 100.0) / 100.0

    if stake < min_stake or free < min_stake:
        return None, free
    return stake, free
