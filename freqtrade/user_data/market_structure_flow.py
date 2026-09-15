from __future__ import annotations

import json
import math
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASES = (
    'https://data-api.binance.vision/api/v3',
    'https://api.binance.com/api/v3',
    'https://api-gcp.binance.com/api/v3',
)
MIN_SCORE = float(os.getenv('FLOW_MIN_SCORE', '50'))
SIDEWAYS_MIN_SCORE = float(os.getenv('FLOW_SIDEWAYS_MIN_SCORE', '55'))
WEAK_BEAR_MIN_SCORE = float(os.getenv('FLOW_WEAK_BEAR_MIN_SCORE', '60'))
MAX_STOP_RISK_USDT = float(os.getenv('FLOW_MAX_STOP_RISK_USDT', '0.50'))
MIN_EXEC_STAKE_USDT = float(os.getenv('FLOW_MIN_EXEC_STAKE_USDT', '5.50'))
WEAK_BEAR_STAKE_MULT = max(0.50, min(0.85, float(os.getenv('FLOW_WEAK_BEAR_STAKE_MULT', '0.70'))))
WEAK_BEAR_MAX_STAKE_USDT = max(MIN_EXEC_STAKE_USDT, float(os.getenv('FLOW_WEAK_BEAR_MAX_STAKE_USDT', '10.00')))
FAIL_CLOSED = str(os.getenv('FLOW_FAIL_CLOSED', '1')).lower() not in {'0', 'false', 'no', 'off'}


def _get(url: str, timeout: int = 8):
    req = Request(url, headers={'User-Agent': 'tst-structure-flow/1.1', 'Accept': 'application/json'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'null')


def _api(path: str, params: dict):
    q = urlencode(params)
    last = None
    for base in BASES:
        try:
            return _get(f'{base}{path}?{q}')
        except Exception as exc:
            last = exc
    raise RuntimeError(f'public-market-data-unavailable:{type(last).__name__}:{str(last)[:100]}')


def _closed(symbol: str) -> list[list]:
    rows = _api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 120})
    now = int(time.time() * 1000)
    out = [x for x in rows if isinstance(x, list) and len(x) > 10 and int(x[6]) < now] if isinstance(rows, list) else []
    if len(out) < 90:
        raise RuntimeError(f'insufficient-closed-klines:{len(out)}')
    return out


def _atr_pct(rows: list[list]) -> float:
    c = [float(x[4]) for x in rows]
    h = [float(x[2]) for x in rows]
    l = [float(x[3]) for x in rows]
    if len(rows) < 21 or c[-1] <= 0:
        return 0.0
    tr = []
    for i in range(len(rows) - 20, len(rows)):
        prev = c[i - 1]
        tr.append(max(h[i] - l[i], abs(h[i] - prev), abs(l[i] - prev)))
    return sum(tr) / len(tr) / c[-1]


def volume_profile(rows: list[list], bins: int = 24) -> dict:
    sample = rows[-90:]
    lo = min(float(x[3]) for x in sample)
    hi = max(float(x[2]) for x in sample)
    if not (hi > lo > 0):
        return {'poc': 0.0, 'val': 0.0, 'vah': 0.0}
    width = (hi - lo) / bins
    vols = [0.0] * bins
    for x in sample:
        typical = (float(x[2]) + float(x[3]) + float(x[4])) / 3.0
        idx = max(0, min(bins - 1, int((typical - lo) / width)))
        vols[idx] += max(0.0, float(x[7]))
    total = sum(vols)
    if total <= 0:
        return {'poc': (lo + hi) / 2.0, 'val': lo, 'vah': hi}
    poc_i = max(range(bins), key=lambda i: vols[i])
    left = right = poc_i
    covered = vols[poc_i]
    while covered < total * 0.70 and (left > 0 or right < bins - 1):
        lv = vols[left - 1] if left > 0 else -1.0
        rv = vols[right + 1] if right < bins - 1 else -1.0
        if rv > lv:
            right += 1; covered += max(0.0, rv)
        else:
            left -= 1; covered += max(0.0, lv)
    return {
        'poc': lo + (poc_i + 0.5) * width,
        'val': lo + left * width,
        'vah': lo + (right + 1) * width,
    }


def _wyckoff(rows: list[list], strong_flow: bool, taker: float) -> dict:
    prior = rows[-60:-10]
    recent = rows[-10:]
    rh = max(float(x[2]) for x in prior)
    rl = min(float(x[3]) for x in prior)
    last = float(recent[-1][4])
    recent_high = max(float(x[2]) for x in recent)
    recent_low = min(float(x[3]) for x in recent)
    prior_qv = sum(float(x[7]) for x in prior[-20:]) / 20.0
    recent_qv = sum(float(x[7]) for x in recent[-5:]) / 5.0
    volx = recent_qv / prior_qv if prior_qv > 0 else 0.0

    spring = recent_low < rl * 0.997 and last > rl * 1.001 and taker >= 0.53
    sos = last > rh * 1.001 and volx >= 1.20 and strong_flow
    breakout_seen = any(float(x[4]) > rh * 1.001 for x in rows[-25:-5])
    lps = breakout_seen and min(float(x[3]) for x in rows[-5:]) >= rh * 0.992 and last > rh and taker >= 0.52

    peak = max(recent, key=lambda x: float(x[2]))
    ph, po, pc = float(peak[2]), float(peak[1]), float(peak[4])
    wick = (ph - max(po, pc)) / max(abs(pc - po), max(last, 1e-12) * 0.0001)
    upthrust = recent_high > rh * 1.003 and last < rh * 0.999 and pc < rh and wick >= 1.5 and taker < 0.55
    phase = 'UPTHRUST' if upthrust else 'SPRING' if spring else 'LPS' if lps else 'SOS' if sos else 'RANGE' if rl <= last <= rh else 'NONE'
    return {'phase': phase, 'spring': spring, 'sos': sos, 'lps': lps, 'upthrust': upthrust, 'range_high': rh, 'range_low': rl}


def collect(symbol: str) -> dict:
    rows = _closed(symbol)
    depth = _api('/depth', {'symbol': symbol, 'limit': 20})
    bids = depth.get('bids') or [] if isinstance(depth, dict) else []
    asks = depth.get('asks') or [] if isinstance(depth, dict) else []
    if not bids or not asks:
        raise RuntimeError('depth-empty')

    bq = sum(float(p) * float(q) for p, q in bids[:20])
    aq = sum(float(p) * float(q) for p, q in asks[:20])
    imbalance = (bq - aq) / (bq + aq) if bq + aq > 0 else 0.0
    bid, ask = float(bids[0][0]), float(asks[0][0])
    bsz, asz = float(bids[0][1]), float(asks[0][1])
    mid = (bid + ask) / 2.0
    micro = ((ask * bsz) + (bid * asz)) / (bsz + asz) if bsz + asz > 0 else mid
    micro_bps = (micro - mid) / mid * 10000.0 if mid > 0 else 0.0

    base = [float(x[5]) for x in rows]
    tb = [float(x[9]) for x in rows]
    rb = sum(base[-5:]); pb = sum(base[-10:-5])
    rt = sum(tb[-5:])
    taker = rt / rb if rb > 0 else 0.5
    d = sum(2 * tb[i] - base[i] for i in range(len(rows) - 5, len(rows))) / rb if rb > 0 else 0.0
    pd = sum(2 * tb[i] - base[i] for i in range(len(rows) - 10, len(rows) - 5)) / pb if pb > 0 else 0.0
    delta_accel = d - pd

    last = float(rows[-1][4])
    mom5 = last / float(rows[-6][4]) - 1.0
    atr = _atr_pct(rows)
    vp = volume_profile(rows)
    vah, val, poc = float(vp['vah']), float(vp['val']), float(vp['poc'])
    ext_atr = ((last - vah) / last) / max(atr, 0.0015) if last > vah > 0 else 0.0
    strong = taker >= 0.56 and (d >= 0.04 or imbalance >= 0.04)
    negative = taker < 0.48 and d < -0.05 and imbalance < -0.08
    absorption = d >= 0.08 and abs(mom5) <= 0.0025 and imbalance >= -0.02
    wy = _wyckoff(rows, strong, taker)
    location = 'ABOVE_VAH' if last > vah > 0 else 'BELOW_VAL' if 0 < last < val else 'ABOVE_POC_IN_VALUE' if last >= poc > 0 else 'BELOW_POC_IN_VALUE'
    return {
        'last': last, 'taker_buy_ratio': taker, 'delta_ratio': d, 'delta_accel': delta_accel,
        'depth_imbalance': imbalance, 'micro_bias_bps': micro_bps, 'strong_buy_flow': strong,
        'negative_flow': negative, 'absorption': absorption, 'atr_pct': atr, 'mom5': mom5,
        'poc': poc, 'val': val, 'vah': vah, 'profile_extension_atr': ext_atr,
        'profile_location': location, **wy,
    }


def decide(m: dict, payload: dict, regime: str = '') -> dict:
    score = 50.0
    reasons = []
    taker = float(m.get('taker_buy_ratio') or 0.5)
    delta = float(m.get('delta_ratio') or 0.0)
    dacc = float(m.get('delta_accel') or 0.0)
    imb = float(m.get('depth_imbalance') or 0.0)
    micro = float(m.get('micro_bias_bps') or 0.0)
    ext = float(m.get('profile_extension_atr') or 0.0)
    loc = str(m.get('profile_location') or '')
    strong = bool(m.get('strong_buy_flow'))
    phase = str(m.get('phase') or 'NONE')

    if taker >= 0.60: score += 10; reasons.append('taker-strong')
    elif taker >= 0.56: score += 7; reasons.append('taker-support')
    elif taker < 0.48: score -= 8; reasons.append('taker-sell')
    if delta >= 0.10: score += 10; reasons.append('cvd-positive')
    elif delta >= 0.04: score += 6; reasons.append('cvd-support')
    elif delta <= -0.08: score -= 10; reasons.append('cvd-negative')
    if dacc >= 0.06: score += 4
    elif dacc <= -0.08: score -= 4
    if imb >= 0.10: score += 7; reasons.append('bid-imbalance')
    elif imb >= 0.02: score += 3
    elif imb <= -0.10: score -= 7; reasons.append('ask-imbalance')
    if micro >= 1.0: score += 3
    elif micro <= -1.0: score -= 3

    if loc == 'ABOVE_POC_IN_VALUE': score += 6; reasons.append('above-poc')
    elif loc == 'BELOW_POC_IN_VALUE': score += 2
    elif loc == 'ABOVE_VAH' and ext <= 1.20 and strong: score += 8; reasons.append('vah-breakout-confirmed')
    elif loc == 'ABOVE_VAH' and ext > 1.60: score -= 12; reasons.append('profile-overextended')
    elif loc == 'BELOW_VAL': score -= 5
    if bool(m.get('absorption')): score += 6; reasons.append('buy-absorption')
    if phase == 'SPRING': score += 14; reasons.append('wyckoff-spring')
    elif phase == 'SOS': score += 12; reasons.append('wyckoff-sos')
    elif phase == 'LPS': score += 10; reasons.append('wyckoff-lps')
    elif phase == 'UPTHRUST': score -= 30; reasons.append('wyckoff-upthrust')
    score = max(0.0, min(100.0, score))

    regime = str(regime or '').upper()
    required = SIDEWAYS_MIN_SCORE if regime == 'SIDEWAYS_COMPRESSION' else WEAK_BEAR_MIN_SCORE if regime in {'WEAK_BEAR', 'PANIC_HIGH_VOL_BEAR'} else MIN_SCORE
    blockers = []
    if bool(m.get('negative_flow')): blockers.append('triple-negative-order-flow')
    if phase == 'UPTHRUST' and not strong: blockers.append('wyckoff-upthrust-trap')
    if loc == 'ABOVE_VAH' and ext > 1.80 and not strong: blockers.append('volume-profile-chase')
    if score < required: blockers.append(f'flow-structure-score<{required:.0f}')

    try:
        entry = float(payload.get('entry') or 0); stop = float(payload.get('stop') or 0); stake = float(payload.get('stakeUSDT') or 0)
    except Exception:
        entry = stop = stake = 0.0
    risk_pct = (entry - stop) / entry if entry > 0 and 0 < stop < entry else 999.0
    atr = float(m.get('atr_pct') or 0.0)
    if risk_pct <= 0 or risk_pct >= 0.03: blockers.append('invalid-structural-risk')
    elif atr > 0 and risk_pct < max(0.0035, 0.55 * atr): blockers.append('stop-inside-market-noise')

    mult = 1.0 if score >= 75 else 0.85 if score >= 60 else 0.70
    if regime == 'WEAK_BEAR' and stake > 0:
        # Weak bear is selective, not a blanket veto. Keep exceptional setups
        # eligible while automatically shrinking dollars exposed. Never increase
        # a stake and never shrink below Binance's executable minimum solely due
        # to this regime adjustment.
        executable_floor_mult = min(1.0, MIN_EXEC_STAKE_USDT / stake)
        mult = min(mult, max(WEAK_BEAR_STAKE_MULT, executable_floor_mult))
        reasons.append('weak-bear-risk-reduction')

    adjusted = stake * mult if stake > 0 else 0.0
    if 0 < risk_pct < 1:
        adjusted = min(adjusted, MAX_STOP_RISK_USDT / risk_pct)
    if regime == 'WEAK_BEAR' and adjusted > 0:
        adjusted = min(adjusted, WEAK_BEAR_MAX_STAKE_USDT)
    adjusted = math.floor(max(0.0, adjusted) * 100.0) / 100.0
    if stake >= MIN_EXEC_STAKE_USDT and adjusted < MIN_EXEC_STAKE_USDT:
        blockers.append('risk-sized-below-min-executable-stake')
    if stake > 0:
        adjusted = min(stake, max(MIN_EXEC_STAKE_USDT, adjusted))
    return {
        'passed': not blockers,
        'status': 'FLOW_STRUCTURE_PASS' if not blockers else 'FLOW_STRUCTURE_REJECT',
        'reason': 'ok' if not blockers else '|'.join(blockers),
        'quality_score': round(score, 2), 'required_score': required,
        'stake_multiplier': mult, 'adjusted_stake_usdt': round(adjusted, 2),
        'risk_pct': risk_pct, 'reasons': reasons, 'metrics': m,
    }


def evaluate(symbol: str, payload: dict, regime: str = '') -> dict:
    try:
        return decide(collect(symbol), payload, regime)
    except Exception as exc:
        return {
            'passed': not FAIL_CLOSED, 'status': 'FLOW_DATA_UNAVAILABLE',
            'reason': f'{type(exc).__name__}:{str(exc)[:160]}', 'quality_score': None,
            'required_score': None, 'adjusted_stake_usdt': float(payload.get('stakeUSDT') or 0.0), 'metrics': {},
        }
