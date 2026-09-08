from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def free_usdt() -> float:
    key = (os.getenv('BINANCE_API_KEY') or '').strip()
    secret = (os.getenv('BINANCE_API_SECRET') or '').strip()
    if not key or not secret:
        raise RuntimeError('BINANCE_CREDENTIALS_MISSING_FOR_SIZING')

    last_error = 'unavailable'
    for base in API_BASES:
        params = {'recvWindow': 5000, 'timestamp': int(time.time() * 1000)}
        query = urlencode(params)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        req = Request(
            f'{base}/api/v3/account?{query}&signature={signature}',
            headers={'X-MBX-APIKEY': key, 'User-Agent': 'tst-dynamic-sizing/1.0'},
        )
        try:
            with urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            for balance in data.get('balances') or []:
                if balance.get('asset') == 'USDT':
                    return max(0.0, float(balance.get('free') or 0.0))
            return 0.0
        except Exception as exc:
            last_error = f'{type(exc).__name__}:{exc}'
    raise RuntimeError(f'BINANCE_BALANCE_READ_FAILED:{last_error}')


def recommended_stake(stop_pct: float) -> tuple[float | None, float]:
    """Return (stake_usdt, free_usdt).

    Position size is capped by available balance, configured max stake fraction,
    per-trade risk budget and the hard 10 USDT execution ceiling.
    """
    free = free_usdt()
    min_stake = max(5.0, _env_float('MIN_STAKE_USDT', 5.0))
    max_stake_fraction = min(max(_env_float('MAX_STAKE_FRACTION', 0.50), 0.01), 0.95)
    risk_fraction = min(max(_env_float('RISK_FRACTION_OF_BALANCE', 0.02), 0.001), 0.20)
    max_risk = max(0.01, _env_float('MAX_RISK_PER_TRADE_USDT', 0.50))

    stop = max(float(stop_pct), 0.001)
    risk_budget = min(max_risk, free * risk_fraction)
    by_risk = risk_budget / stop
    by_balance = free * max_stake_fraction
    available = max(0.0, free - 0.10)  # leave a small fee/dust buffer
    raw = min(10.0, available, by_balance, by_risk)
    stake = math.floor(raw * 100.0) / 100.0

    if stake < min_stake or free < min_stake:
        return None, free
    return stake, free
