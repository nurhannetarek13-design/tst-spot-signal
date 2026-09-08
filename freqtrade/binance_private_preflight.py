from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KEY = os.getenv('BINANCE_API_KEY', '').strip()
SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
BASES = [
    'https://api.binance.com',
    'https://api-gcp.binance.com',
    'https://api1.binance.com',
    'https://api2.binance.com',
    'https://api3.binance.com',
    'https://api4.binance.com',
]

if not KEY or not SECRET:
    print('[private-preflight] missing Binance credentials')
    raise SystemExit(0)

for base in BASES:
    params = {'recvWindow': 5000, 'timestamp': int(time.time() * 1000)}
    query = urlencode(params)
    sig = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    req = Request(f'{base}/api/v3/account?{query}&signature={sig}', headers={'X-MBX-APIKEY':KEY,'User-Agent':'tst-private-preflight/1.0'})
    try:
        with urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        print(f'[private-preflight] OK base={base} canTrade={bool(data.get("canTrade"))} accountType={data.get("accountType")}')
        break
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode())
            code = data.get('code')
            msg = str(data.get('msg') or '')[:100]
        except Exception:
            code = None
            msg = ''
        print(f'[private-preflight] FAIL base={base} http={exc.code} code={code} msg={msg}')
    except Exception as exc:
        print(f'[private-preflight] FAIL base={base} error={type(exc).__name__}:{exc}')
