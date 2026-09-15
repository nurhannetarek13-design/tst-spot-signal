from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'def _market_structure_flow_gate(' in s:
    print('[market-structure-flow-v1] already applied')
    raise SystemExit(0)

helper_marker = '\ndef _expert_pre_ingest(payload: dict) -> bool:\n'
helpers = r'''

# Final confirmation layer for live Spot entries.
# Uses only public Binance Spot data and NEVER increases stake/risk.
MSF_ENABLED = os.getenv('FAST_MARKET_STRUCTURE_FLOW_ENABLED', '1').strip() == '1'
MSF_MIN_SCORE = max(0.0, min(100.0, float(os.getenv('FAST_MARKET_STRUCTURE_FLOW_MIN_SCORE', '50'))))
MSF_CACHE_SEC = max(5, int(os.getenv('FAST_MARKET_STRUCTURE_FLOW_CACHE_SEC', '20')))
MSF_DEPTH_BAND_PCT = max(0.001, min(0.01, float(os.getenv('FAST_ORDERFLOW_DEPTH_BAND_PCT', '0.0035'))))
MSF_HARD_SELL_DELTA = max(-0.95, min(-0.05, float(os.getenv('FAST_ORDERFLOW_HARD_SELL_DELTA', '-0.18'))))
MSF_HARD_DEPTH_RATIO = max(0.20, min(1.0, float(os.getenv('FAST_ORDERFLOW_HARD_DEPTH_RATIO', '0.85'))))
MSF_MAX_VAH_EXTENSION = max(0.004, min(0.03, float(os.getenv('FAST_VOLUME_PROFILE_MAX_VAH_EXTENSION', '0.0125'))))
_msf_cache: dict[str, tuple[float, bool, dict]] = {}


def _median(values):
    xs = sorted(float(x) for x in values if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0


def _volume_profile_from_trades(trades: list, bins: int = 24) -> dict:
    pts = []
    for t in trades or []:
        try:
            p = float(t.get('p') or 0.0)
            q = float(t.get('q') or 0.0)
            if p > 0 and q > 0:
                pts.append((p, p * q))
        except Exception:
            continue
    if len(pts) < 20:
        raise RuntimeError('volume-profile-insufficient-trades')
    lo = min(p for p, _ in pts)
    hi = max(p for p, _ in pts)
    if not hi > lo:
        return {'poc': lo, 'val': lo, 'vah': hi, 'total_quote': sum(v for _, v in pts), 'bins': 1}
    bins = max(12, min(48, int(bins)))
    width = (hi - lo) / bins
    vols = [0.0] * bins
    for p, v in pts:
        idx = min(bins - 1, max(0, int((p - lo) / width)))
        vols[idx] += v
    total = sum(vols)
    if total <= 0:
        raise RuntimeError('volume-profile-zero-volume')
    poc_i = max(range(bins), key=lambda i: vols[i])
    selected = {poc_i}
    acc = vols[poc_i]
    left = poc_i - 1
    right = poc_i + 1
    target = total * 0.70
    while acc < target and (left >= 0 or right < bins):
        lv = vols[left] if left >= 0 else -1.0
        rv = vols[right] if right < bins else -1.0
        if rv > lv:
            selected.add(right); acc += max(0.0, rv); right += 1
        else:
            selected.add(left); acc += max(0.0, lv); left -= 1
    low_i = min(selected)
    high_i = max(selected)
    return {
        'poc': lo + (poc_i + 0.5) * width,
        'val': lo + low_i * width,
        'vah': lo + (high_i + 1) * width,
        'total_quote': total,
        'bins': bins,
    }


def _orderflow_from_trades(trades: list) -> dict:
    buy_q = 0.0
    sell_q = 0.0
    for t in trades or []:
        try:
            p = float(t.get('p') or 0.0)
            q = float(t.get('q') or 0.0)
            notional = p * q
            if notional <= 0:
                continue
            # Binance aggTrade: m=True => buyer is maker => aggressive seller.
            if bool(t.get('m')):
                sell_q += notional
            else:
                buy_q += notional
        except Exception:
            continue
    total = buy_q + sell_q
    if total <= 0:
        raise RuntimeError('orderflow-zero-volume')
    return {
        'taker_buy_ratio': buy_q / total,
        'delta': (buy_q - sell_q) / total,
        'buy_quote': buy_q,
        'sell_quote': sell_q,
        'total_quote': total,
    }


def _depth_imbalance(book: dict, band_pct: float = MSF_DEPTH_BAND_PCT) -> dict:
    bids = book.get('bids') or []
    asks = book.get('asks') or []
    if not bids or not asks:
        raise RuntimeError('depth-empty')
    best_bid = float(bids[0][0]); best_ask = float(asks[0][0])
    if not (best_ask >= best_bid > 0):
        raise RuntimeError('depth-bad-top')
    mid = (best_bid + best_ask) / 2.0
    bid_floor = mid * (1.0 - band_pct)
    ask_ceil = mid * (1.0 + band_pct)
    bid_notional = sum(float(p) * float(q) for p, q in bids if float(p) >= bid_floor)
    ask_notional = sum(float(p) * float(q) for p, q in asks if float(p) <= ask_ceil)
    ratio = bid_notional / ask_notional if ask_notional > 0 else 9.0
    return {
        'mid': mid,
        'bid_notional': bid_notional,
        'ask_notional': ask_notional,
        'imbalance': ratio,
    }


def _wyckoff_state(rows: list) -> dict:
    now_ms = int(time.time() * 1000)
    clean = [x for x in (rows or []) if len(x) > 7 and int(x[6]) < now_ms]
    if len(clean) < 55:
        raise RuntimeError('wyckoff-insufficient-bars')
    opens = [float(x[1]) for x in clean]
    highs = [float(x[2]) for x in clean]
    lows = [float(x[3]) for x in clean]
    closes = [float(x[4]) for x in clean]
    qv = [float(x[7]) for x in clean]

    base_hi = max(highs[-55:-15])
    base_lo = min(lows[-55:-15])
    base_vol = _median(qv[-55:-15]) or 1e-12
    recent_hi = max(highs[-5:])
    recent_lo = min(lows[-5:])
    close = closes[-1]
    last_vol = qv[-1]
    prev_breakout = max(closes[-14:-3]) > base_hi * 1.0015
    pullback_low = min(lows[-3:])
    pullback_vol = sum(qv[-3:]) / 3.0
    pre_pullback_vol = sum(qv[-14:-3]) / max(1, len(qv[-14:-3]))

    spring = recent_lo < base_lo * 0.9985 and close > base_lo and last_vol >= base_vol * 1.10
    upthrust = recent_hi > base_hi * 1.0015 and close < base_hi and last_vol >= base_vol * 1.10
    sos = close > base_hi * 1.0015 and last_vol >= base_vol * 1.20
    lps = (
        prev_breakout and
        pullback_low <= base_hi * 1.004 and
        close >= base_hi * 0.999 and
        pullback_vol <= pre_pullback_vol * 0.90
    )

    if upthrust:
        state = 'UPTHRUST'
    elif spring:
        state = 'SPRING'
    elif sos:
        state = 'SOS'
    elif lps:
        state = 'LPS'
    else:
        state = 'NEUTRAL'
    return {
        'state': state,
        'base_hi': base_hi,
        'base_lo': base_lo,
        'close': close,
        'volume_multiple': last_vol / base_vol if base_vol > 0 else 0.0,
    }


def _market_structure_flow_gate(symbol: str, payload: dict) -> tuple[bool, dict]:
    if not MSF_ENABLED:
        return True, {'status': 'DISABLED', 'score': None, 'risk_mult': 1.0}
    now = time.time()
    cached = _msf_cache.get(symbol)
    if cached and now - cached[0] <= MSF_CACHE_SEC:
        return cached[1], dict(cached[2])
    try:
        trades = api('/aggTrades', {'symbol': symbol, 'limit': 1000})
        depth = api('/depth', {'symbol': symbol, 'limit': 100})
        rows = api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 90})
        if not isinstance(trades, list) or not isinstance(depth, dict) or not isinstance(rows, list):
            raise RuntimeError('bad-public-market-data-shape')
        flow = _orderflow_from_trades(trades)
        book = _depth_imbalance(depth)
        profile = _volume_profile_from_trades(trades)
        wy = _wyckoff_state(rows)
        live = float(payload.get('entry') or book.get('mid') or wy.get('close') or 0.0)
        if live <= 0:
            raise RuntimeError('bad-entry-price')

        score = 0.0
        reasons = []
        taker = float(flow['taker_buy_ratio'])
        delta = float(flow['delta'])
        imb = float(book['imbalance'])

        if taker >= 0.60 and delta >= 0.20:
            score += 30; reasons.append('aggressive-buy-flow')
        elif taker >= 0.56 and delta >= 0.10:
            score += 24; reasons.append('buy-flow')
        elif taker >= 0.53 and delta >= 0.02:
            score += 14; reasons.append('mild-buy-flow')
        elif taker < 0.47 and delta < -0.06:
            score -= 12; reasons.append('sell-flow')

        if imb >= 1.25:
            score += 18; reasons.append('bid-depth-strong')
        elif imb >= 1.08:
            score += 12; reasons.append('bid-depth')
        elif imb >= 0.95:
            score += 6; reasons.append('depth-balanced')
        elif imb < 0.75:
            score -= 12; reasons.append('ask-depth-heavy')

        poc = float(profile['poc']); val = float(profile['val']); vah = float(profile['vah'])
        above_vah = (live / vah - 1.0) if vah > 0 else 9.0
        if val <= live <= vah:
            if live >= poc:
                score += 18; reasons.append('accepted-above-poc')
            else:
                score += 12; reasons.append('inside-value')
        elif live > vah and above_vah <= 0.006 and delta >= 0.06:
            score += 18; reasons.append('value-area-breakout')
        elif live > vah and above_vah > MSF_MAX_VAH_EXTENSION:
            score -= 15; reasons.append('profile-overextended')
        elif live < val and wy.get('state') == 'SPRING':
            score += 10; reasons.append('spring-below-value')

        wstate = str(wy.get('state') or 'NEUTRAL')
        if wstate in ('SPRING', 'SOS'):
            score += 22; reasons.append('wyckoff-' + wstate.lower())
        elif wstate == 'LPS':
            score += 18; reasons.append('wyckoff-lps')
        elif wstate == 'UPTHRUST':
            score -= 30; reasons.append('wyckoff-upthrust')

        hard_reason = None
        if delta <= MSF_HARD_SELL_DELTA and imb < MSF_HARD_DEPTH_RATIO:
            hard_reason = 'toxic-sell-flow+ask-depth'
        elif wstate == 'UPTHRUST' and delta < 0.05:
            hard_reason = 'wyckoff-upthrust-no-buy-confirmation'
        elif above_vah > MSF_MAX_VAH_EXTENSION and taker < 0.60:
            hard_reason = 'volume-profile-overextension-without-flow'

        score = max(0.0, min(100.0, score))
        risk_mult = 1.0 if score >= 75 else (0.90 if score >= 60 else 0.75)
        passed = hard_reason is None and score >= MSF_MIN_SCORE
        status = 'PASS' if passed else ('HARD_REJECT' if hard_reason else 'QUALITY_REJECT')
        meta = {
            'status': status,
            'score': round(score, 2),
            'required': MSF_MIN_SCORE,
            'risk_mult': risk_mult,
            'taker_buy_ratio': round(taker, 4),
            'delta': round(delta, 4),
            'depth_imbalance': round(imb, 4),
            'poc': poc, 'val': val, 'vah': vah,
            'profile_extension': round(above_vah, 5),
            'wyckoff': wstate,
            'reasons': reasons,
            'reject_reason': hard_reason,
        }
        _msf_cache[symbol] = (now, passed, dict(meta))
        return passed, meta
    except Exception as exc:
        meta = {
            'status': 'DATA_REJECT', 'score': None, 'required': MSF_MIN_SCORE,
            'risk_mult': 0.0, 'reject_reason': f'{type(exc).__name__}:{str(exc)[:120]}',
        }
        _msf_cache[symbol] = (now, False, dict(meta))
        return False, meta

'''

if helper_marker not in s:
    raise SystemExit('market-structure-flow-v1: expert gate marker missing')
s = s.replace(helper_marker, helpers + helper_marker, 1)

portfolio_marker = "    ok, why = _portfolio_allows(payload)\n"
flow_block = r'''    flow_ok, flow_meta = _market_structure_flow_gate(symbol, payload)
    payload['marketStructureFlow'] = flow_meta
    if not flow_ok:
        flow_reason = str(flow_meta.get('reject_reason') or flow_meta.get('status') or 'reject')
        print(
            f"[market-structure-flow] {symbol} BLOCKED score={flow_meta.get('score')} "
            f"delta={flow_meta.get('delta')} taker={flow_meta.get('taker_buy_ratio')} "
            f"depth={flow_meta.get('depth_imbalance')} vp={flow_meta.get('profile_extension')} "
            f"wyckoff={flow_meta.get('wyckoff')} reason={flow_reason}", flush=True,
        )
        _record_candidate(symbol, lane, score, price, 'REJECT', 'market-structure-flow:' + flow_reason, market_structure_flow=flow_meta)
        return False

    # Risk management: this layer may only REDUCE stake. It never scales risk up.
    old_stake = float(payload.get('stakeUSDT') or 0.0)
    risk_mult = max(0.0, min(1.0, float(flow_meta.get('risk_mult') or 1.0)))
    if old_stake > 0 and risk_mult < 1.0:
        payload['stakeUSDT'] = math.floor(old_stake * risk_mult * 100.0) / 100.0
    print(
        f"[market-structure-flow] {symbol} PASS score={flow_meta.get('score')} "
        f"delta={flow_meta.get('delta')} taker={flow_meta.get('taker_buy_ratio')} "
        f"depth={flow_meta.get('depth_imbalance')} poc={flow_meta.get('poc')} "
        f"vah={flow_meta.get('vah')} wyckoff={flow_meta.get('wyckoff')} "
        f"stake={old_stake:.2f}->{float(payload.get('stakeUSDT') or 0.0):.2f}", flush=True,
    )

    ok, why = _portfolio_allows(payload)
'''

# Target only the FINAL Spot Sniper wrapper: it is the first portfolio call after
# the live-contract reject block. Earlier legacy helper is renamed and unused.
wrapper_pos = s.find('def _expert_pre_ingest(payload: dict) -> bool:\n')
if wrapper_pos < 0:
    raise SystemExit('market-structure-flow-v1: final expert wrapper missing')
portfolio_pos = s.find(portfolio_marker, wrapper_pos)
if portfolio_pos < 0:
    raise SystemExit('market-structure-flow-v1: final portfolio marker missing')
s = s[:portfolio_pos] + flow_block + s[portfolio_pos + len(portfolio_marker):]

startup_marker = "    last_chat_retry = 0.0\n"
startup = "    print(f'[market-structure-flow-v1] ONLINE orderflow=aggTrades+depth volume_profile=70%VA wyckoff=rules risk=reduce-only min_score={MSF_MIN_SCORE:.0f}')\n"
if '[market-structure-flow-v1] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('market-structure-flow-v1: startup marker missing')
    s = s.replace(startup_marker, startup + startup_marker, 1)

for marker in [
    'def _market_structure_flow_gate(',
    "api('/aggTrades'",
    "api('/depth'",
    "'marketStructureFlow'",
    '[market-structure-flow] ',
    'risk_mult < 1.0',
]:
    if marker not in s:
        raise SystemExit(f'market-structure-flow-v1: missing marker {marker}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[market-structure-flow-v1] OK live order flow + 70% volume profile + rules-based Wyckoff + reduce-only risk sizing')
