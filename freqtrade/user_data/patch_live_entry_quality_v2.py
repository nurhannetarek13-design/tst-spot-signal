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

# Final market-location/flow gate. It runs only after persistence + Spot Sniper
# authorization, so it improves entry quality without burdening every scanner
# candidate. Order-book depth is never trusted alone: hard rejection needs a
# multi-signal trap (taker delta + depth, Wyckoff upthrust, or profile chase).
engine_path = Path('/freqtrade/fast_entry_engine.py')
s = engine_path.read_text(encoding='utf-8')
import_marker = 'import spot_sniper_gate\n'
if 'from user_data import market_structure_flow\n' not in s:
    if import_marker not in s:
        raise SystemExit('live-entry-quality-v2: spot sniper import marker missing')
    s = s.replace(import_marker, import_marker + 'from user_data import market_structure_flow\n', 1)

anchor = "    ok, why = _portfolio_allows(payload)\n    if not ok:\n"
flow_block = r'''    flow = market_structure_flow.evaluate(symbol, payload, str(decision.get('regime') or ''))
    payload['marketStructureFlow'] = flow
    fm = flow.get('metrics') or {}
    telemetry.update({
        'flow_structure_status': flow.get('status'),
        'flow_structure_score': flow.get('quality_score'),
        'flow_structure_required': flow.get('required_score'),
        'flow_structure_reason': flow.get('reason'),
        'flow_taker_buy_ratio': fm.get('taker_buy_ratio'),
        'flow_delta_ratio': fm.get('delta_ratio'),
        'flow_depth_imbalance': fm.get('depth_imbalance'),
        'flow_micro_bias_bps': fm.get('micro_bias_bps'),
        'volume_profile_location': fm.get('profile_location'),
        'volume_profile_poc': fm.get('poc'),
        'volume_profile_val': fm.get('val'),
        'volume_profile_vah': fm.get('vah'),
        'wyckoff_phase': fm.get('phase'),
    })
    if not bool(flow.get('passed')):
        why_flow = 'market-structure-flow:' + str(flow.get('reason') or flow.get('status') or 'reject')
        print(
            f"[structure-flow] {symbol} BLOCKED lane={lane} regime={decision.get('regime')} "
            f"quality={flow.get('quality_score')} required={flow.get('required_score')} "
            f"phase={fm.get('phase')} profile={fm.get('profile_location')} "
            f"taker={fm.get('taker_buy_ratio')} delta={fm.get('delta_ratio')} "
            f"depth={fm.get('depth_imbalance')} reason={why_flow}", flush=True,
        )
        _record_candidate(symbol, lane, score, price, 'REJECT', why_flow, **telemetry)
        return False

    try:
        old_stake = float(payload.get('stakeUSDT') or 0.0)
        new_stake = float(flow.get('adjusted_stake_usdt') or old_stake)
        if old_stake > 0 and 0 < new_stake < old_stake:
            payload['stakeUSDT'] = new_stake
            telemetry['stake_usdt'] = new_stake
            telemetry['flow_original_stake_usdt'] = old_stake
    except Exception:
        pass
    print(
        f"[structure-flow] {symbol} PASS lane={lane} regime={decision.get('regime')} "
        f"quality={flow.get('quality_score')}/{flow.get('required_score')} "
        f"phase={fm.get('phase')} profile={fm.get('profile_location')} "
        f"taker={float(fm.get('taker_buy_ratio') or 0)*100:.1f}% "
        f"delta={float(fm.get('delta_ratio') or 0):+.3f} depth={float(fm.get('depth_imbalance') or 0):+.3f} "
        f"stake={payload.get('stakeUSDT')}", flush=True,
    )

    ok, why = _portfolio_allows(payload)
    if not ok:
'''
if '[structure-flow] ' not in s:
    wrapper = s.find('def _expert_pre_ingest(payload: dict) -> bool:\n')
    if wrapper < 0:
        raise SystemExit('live-entry-quality-v2: final expert wrapper missing')
    pos = s.find(anchor, wrapper)
    if pos < 0:
        raise SystemExit('live-entry-quality-v2: portfolio anchor missing in final wrapper')
    s = s[:pos] + flow_block + s[pos + len(anchor):]

startup_marker = "    last_chat_retry = 0.0\n"
startup = "    print(f'[market-structure-flow] ONLINE order_flow=depth20+taker-delta volume_profile=90m/70pct wyckoff=objective risk_quality_sizing=ON fail_closed={market_structure_flow.FAIL_CLOSED}')\n"
if '[market-structure-flow] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('live-entry-quality-v2: structure-flow startup marker missing')
    s = s.replace(startup_marker, startup + startup_marker, 1)

for marker in ['from user_data import market_structure_flow', "payload['marketStructureFlow']", '[structure-flow] ', '[market-structure-flow] ONLINE']:
    if marker not in s:
        raise SystemExit(f'live-entry-quality-v2: structure-flow marker missing {marker}')
compile(s, str(engine_path), 'exec')
engine_path.write_text(s, encoding='utf-8')

# Build-time pure decision tests: no external API required.
from user_data import market_structure_flow as _msf
_good = {
    'taker_buy_ratio': 0.60, 'delta_ratio': 0.10, 'delta_accel': 0.03,
    'depth_imbalance': 0.10, 'micro_bias_bps': 1.2, 'strong_buy_flow': True,
    'negative_flow': False, 'absorption': False, 'profile_location': 'ABOVE_POC_IN_VALUE',
    'profile_extension_atr': 0.0, 'phase': 'RANGE', 'atr_pct': 0.0025,
}
_bad = dict(_good)
_bad.update({'taker_buy_ratio': 0.44, 'delta_ratio': -0.14, 'depth_imbalance': -0.18, 'strong_buy_flow': False, 'negative_flow': True})
_p = {'entry': 100.0, 'stop': 99.0, 'target': 102.0, 'stakeUSDT': 20.0}
assert _msf.decide(_good, _p, 'WEAK_BULL')['passed'] is True
assert _msf.decide(_bad, _p, 'WEAK_BULL')['passed'] is False
_up = dict(_good); _up.update({'phase': 'UPTHRUST', 'strong_buy_flow': False, 'taker_buy_ratio': 0.51})
assert _msf.decide(_up, _p, 'SIDEWAYS_COMPRESSION')['passed'] is False

print('[live-entry-quality-v2] OK live RR>=1.45 + order-flow/CVD + volume-profile + objective-Wyckoff + quality-aware risk sizing')
