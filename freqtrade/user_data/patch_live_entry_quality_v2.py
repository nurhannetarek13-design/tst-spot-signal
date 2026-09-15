from pathlib import Path

proxy_path = Path('/freqtrade/front_proxy.py')
p = proxy_path.read_text(encoding='utf-8')

# Revalidate reward/risk against the live Binance price immediately before a
# MARKET BUY. A setup can be valid at signal time and still become stale while
# confirmation/execution is in flight. Fail closed if the remaining payoff is
# no longer good enough.
const_marker = "try:\n    MAX_SIGNAL_AGE_SEC=max(15,int(os.getenv('MAX_SIGNAL_AGE_SEC','120')))\nexcept Exception:\n    MAX_SIGNAL_AGE_SEC=120\n"
const_extra = "\ntry:\n    MIN_LIVE_ENTRY_RR=max(1.0,float(os.getenv('MIN_LIVE_ENTRY_RR','1.45')))\nexcept Exception:\n    MIN_LIVE_ENTRY_RR=1.45\n\nPUBLIC_TICKER_BASES=(\n    'https://api.binance.com/api/v3',\n    'https://api-gcp.binance.com/api/v3',\n    'https://api1.binance.com/api/v3',\n    'https://api2.binance.com/api/v3',\n)\n"
if 'MIN_LIVE_ENTRY_RR=' not in p:
    if const_marker not in p:
        raise SystemExit('live-entry-quality-v2: proxy constants marker missing')
    p = p.replace(const_marker, const_marker + const_extra, 1)

helper_marker = "\ndef _client_id(prefix: str, signal_id: str) -> str:\n"
helper = r'''

def _live_price(symbol: str) -> float:
    last = None
    for base in PUBLIC_TICKER_BASES:
        try:
            req = urllib.request.Request(
                f'{base}/ticker/price?symbol={symbol}',
                headers={'User-Agent':'tst-live-entry-quality/2.1','Accept':'application/json'},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                row = json.loads(r.read() or b'{}')
            px = float(row.get('price') or 0.0) if isinstance(row, dict) else 0.0
            if px > 0:
                return px
        except Exception as exc:
            last = exc
    raise RuntimeError(f'LIVE_TICKER_UNAVAILABLE:{type(last).__name__}:{str(last)[:100]}')


def _live_rr_check(symbol: str, body: dict):
    try:
        tp = float(body.get('take_profit_price') or 0.0)
        sl = float(body.get('stop_loss_price') or 0.0)
    except Exception:
        return False, {'status':'LIVE_RR_INPUT_INVALID'}
    if not (tp > sl > 0):
        return False, {'status':'LIVE_RR_INPUT_INVALID'}
    try:
        px = _live_price(symbol)
    except Exception as exc:
        return False, {'status':'LIVE_PRICE_UNAVAILABLE','reason':str(exc)[:140]}
    if not (sl < px < tp):
        return False, {
            'status':'LIVE_PRICE_GEOMETRY_REJECT',
            'livePrice':px,'takeProfit':tp,'stopLoss':sl,
        }
    reward = tp - px
    risk = px - sl
    rr = reward / risk if risk > 0 else 0.0
    if rr < MIN_LIVE_ENTRY_RR:
        return False, {
            'status':'LIVE_RR_REJECT',
            'livePrice':px,'takeProfit':tp,'stopLoss':sl,
            'liveRR':round(rr,4),'requiredRR':MIN_LIVE_ENTRY_RR,
        }
    return True, {
        'livePrice':px,'liveRR':round(rr,4),'requiredRR':MIN_LIVE_ENTRY_RR,
    }
'''
if 'def _live_rr_check(' not in p:
    if helper_marker not in p:
        raise SystemExit('live-entry-quality-v2: proxy helper marker missing')
    p = p.replace(helper_marker, helper + helper_marker, 1)

buy_old = """                quote,filter_meta=binance_filters.normalize_buy_quote(symbol,quote)\n                body['quote_amount_usdt']=quote\n                body['execution_filter_meta']=filter_meta\n                # Exchange-level exact-once key. Make maps this to Binance\n"""
buy_new = """                quote,filter_meta=binance_filters.normalize_buy_quote(symbol,quote)\n                live_ok,live_meta=_live_rr_check(symbol,body)\n                if not live_ok:\n                    trade_state.append_event('LIVE_RR_BLOCK',signal_id=signal_id,symbol=symbol,**live_meta)\n                    print(f\"[live-rr] BLOCK {symbol} signal={signal_id} status={live_meta.get('status')} rr={live_meta.get('liveRR')} required={MIN_LIVE_ENTRY_RR}\",flush=True)\n                    return self.send_json(409,{'ok':False,**live_meta,'signal_id':signal_id,'symbol':symbol})\n                body['quote_amount_usdt']=quote\n                body['execution_filter_meta']={**filter_meta,**live_meta}\n                # Exchange-level exact-once key. Make maps this to Binance\n"""
if '[live-rr] BLOCK' not in p:
    if buy_old not in p:
        raise SystemExit('live-entry-quality-v2: BUY insertion marker missing')
    p = p.replace(buy_old, buy_new, 1)

for marker in ['MIN_LIVE_ENTRY_RR=', 'def _live_rr_check(', '[live-rr] BLOCK']:
    if marker not in p:
        raise SystemExit(f'live-entry-quality-v2: missing proxy marker {marker}')

compile(p, str(proxy_path), 'exec')
proxy_path.write_text(p, encoding='utf-8')
print('[live-entry-quality-v2] OK execution-time live RR>=1.45 fail-closed before BUY')
