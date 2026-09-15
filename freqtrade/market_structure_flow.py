from __future__ import annotations

import json
import math
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BINANCE_BASES = (
    'https://data-api.binance.vision/api/v3',
    'https://api.binance.com/api/v3',
    'https://api-gcp.binance.com/api/v3',
)
FLOW_MIN_SCORE = float(os.getenv('FLOW_MIN_SCORE', '50'))
FLOW_SIDEWAYS_MIN_SCORE = float(os.getenv('FLOW_SIDEWAYS_MIN_SCORE', '55'))
FLOW_WEAK_BEAR_MIN_SCORE = float(os.getenv('FLOW_WEAK_BEAR_MIN_SCORE', '60'))
FLOW_MAX_STOP_RISK_USDT = float(os.getenv('FLOW_MAX_STOP_RISK_USDT', '0.50'))
FLOW_MIN_EXEC_STAKE_USDT = float(os.getenv('FLOW_MIN_EXEC_STAKE_USDT', '5.50'))
FLOW_FAIL_CLOSED = str(os.getenv('FLOW_FAIL_CLOSED', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}


def _get_json(url: str, timeout: int = 8):
    req = Request(url, headers={'User-Agent': 'tst-structure-flow/1.0', 'Accept': 'application/json'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'null')


def _api(path: str, params: dict):
    query = urlencode(params)
    last = None
    for base in BINANCE_BASES:
        try:
            return _get_json(f'{base}{path}?{query}')
        except Exception as exc:
            last = exc
    raise RuntimeError(f'public-market-data-unavailable:{type(last).__name__}:{str(last)[:100]}')


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _closed_klines(symbol: str, limit: int = 120) -> list[list]:
    rows = _api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': limit})
    now_ms = int(time.time() * 1000)
    if not isinstance(rows, list):
        raise RuntimeError('klines-not-list')
    out = [x for x in rows if isinstance(x, list) and len(x) > 10 and int(x[6]) < now_ms]
    if len(out) < 90:
        raise RuntimeError(f'insufficient-closed-klines:{len(out)}')
    return out


def _atr_pct(rows: list[list], lookback: int = 20) -> float:
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    if len(rows) < lookback + 1 or closes[-1] <= 0:
        return 0.0
    trs = []
    for i in range(len(rows) - lookback, len(rows)):
        prev = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return (sum(trs) / max(1, len(trs))) / closes[-1]


def volume_profile(rows: list[list], bins: int = 24, value_area: float = 0.70) -> dict:
    sample = rows[-90:]
    lows = [float(x[3]) for x in sample]
    highs = [float(x[2]) for x in sample]
    lo = min(lows)
    hi = max(highs)
    if not (hi > lo > 0):
        return {'poc': 0.0, 'val': 0.0, 'vah': 0.0, 'bins': bins}
    width = (hi - lo) / bins
    vols = [0.0] * bins
    for x in sample:
        typical = (float(x[2]) + float(x[3]) + float(x[4])) / 3.0
        qv = max(0.0, float(x[7]))
        idx = int((typical - lo) / width) if width > 0 else 0
        idx = max(0, min(bins - 1, idx))
        vols[idx] += qv
    total = sum(vols)
    if total <= 0:
        return {'poc': 0.0, 'val': lo, 'vah': hi, 'bins': bins}
    poc_i = max(range(bins), key=lambda i: vols[i])
    left = right = poc_i
    covered = vols[poc_i]
    target = total * value_area
    while covered < target and (left > 0 or right < bins - 1):
        lv = vols[left - 1] if left > 0 else -1.0
        rv = vols[right + 1] if right < bins - 1 else -1.0
        if rv > lv:
            right += 1
            covered += max(0.0, rv)
        else:
            left -= 1
            covered += max(0.0, lv)
    return {
        'poc': lo + (poc_i + 0.5) * width,
        'val': lo + left * width,
        'vah': lo + (right + 1) * width,
        'profile_low': lo,
        'profile_high': hi,
        'bins': bins,
        'value_coverage': covered / total if total > 0 else 0.0,
    }


def _wyckoff(rows: list[list], strong_buy_flow: bool, taker_buy_ratio: float) -> dict:
    # Objective approximations only; no discretionary chart labels.
    prior = rows[-60:-10]
    recent = rows[-10:]
    if len(prior) < 30 or len(recent) < 5:
        return {'phase': 'NONE', 'spring': False, 'sos': False, 'lps': False, 'upthrust': False}
    range_high = max(float(x[2]) for x in prior)
    range_low = min(float(x[3]) for x in prior)
    last = float(recent[-1][4])
    recent_high = max(float(x[2]) for x in recent)
    recent_low = min(float(x[3]) for x in recent)
    prior_qv = sum(float(x[7]) for x in prior[-20:]) / 20.0
    recent_qv = sum(float(x[7]) for x in recent[-5:]) / 5.0
    volx = recent_qv / prior_qv if prior_qv > 0 else 0.0

    spring = recent_low < range_low * 0.997 and last > range_low * 1.001 and taker_buy_ratio >= 0.53
    sos = last > range_high * 1.001 and volx >= 1.20 and strong_buy_flow

    breakout_seen = any(float(x[4]) > range_high * 1.001 for x in rows[-25:-5])
    hold_low = min(float(x[3]) for x in rows[-5:])
    lps = breakout_seen and hold_low >= range_high * 0.992 and last > range_high and taker_buy_ratio >= 0.52

    peak = max(recent, key=lambda x: float(x[2]))
    peak_high = float(peak[2]); peak_open = float(peak[1]); peak_close = float(peak[4])
    peak_body = abs(peak_close - peak_open)
    peak_upper = peak_high - max(peak_open, peak_close)
    peak_wick_ratio = peak_upper / max(peak_body, max(last, 1e-12) * 0.0001)
    upthrust = (
        recent_high > range_high * 1.003
        and last < range_high * 0.999
        and peak_close < range_high
        and peak_wick_ratio >= 1.5
        and taker_buy_ratio < 0.55
    )

    phase = 'NONE'
    if upthrust: phase = 'UPTHRUST'
    elif spring: phase = 'SPRING'
    elif lps: phase = 'LPS'
    elif sos: phase = 'SOS'
    elif range_low <= last <= range_high: phase = 'RANGE'
    return {
        'phase': phase, 'spring': spring, 'sos': sos, 'lps': lps, 'upthrust': upthrust,
        'range_high': range_high, 'range_low': range_low, 'range_volume_ratio': volx,
        'peak_wick_ratio': peak_wick_ratio,
    }


def collect_metrics(symbol: str) -> dict:
    rows = _closed_klines(symbol, 120)
    depth = _api('/depth', {'symbol': symbol, 'limit': 20})
    bids = depth.get('bids') or [] if isinstance(depth, dict) else []
    asks = depth.get('asks') or [] if isinstance(depth, dict) else []
    if not bids or not asks:
        raise RuntimeError('depth-empty')

    bid_notional = sum(float(p) * float(q) for p, q in bids[:20])
    ask_notional = sum(float(p) * float(q) for p, q in asks[:20])
    depth_total = bid_notional + ask_notional
    depth_imbalance = (bid_notional - ask_notional) / depth_total if depth_total > 0 else 0.0

    bid = float(bids[0][0]); ask = float(asks[0][0])
    bid_qty = float(bids[0][1]); ask_qty = float(asks[0][1])
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(rows[-1][4])
    micro = ((ask * bid_qty) + (bid * ask_qty)) / (bid_qty + ask_qty) if bid_qty + ask_qty > 0 else mid
    micro_bias_bps = ((micro - mid) / mid * 10000.0) if mid > 0 else 0.0

    base = [float(x[5]) for x in rows]
    taker = [float(x[9]) for x in rows]
    closes = [float(x[4]) for x in rows]
    recent_base = sum(base[-5:])
    recent_taker = sum(taker[-5:])
    taker_buy_ratio = recent_taker / recent_base if recent_base > 0 else 0.5
    recent_delta = sum((2.0 * taker[i] - base[i]) for i in range(len(rows) - 5, len(rows)))
    prev_delta = sum((2.0 * taker[i] - base[i]) for i in range(len(rows) - 10, len(rows) - 5))
    delta_ratio = recent_delta / recent_base if recent_base > 0 else 0.0
    prev_base = sum(base[-10:-5])
    prev_delta_ratio = prev_delta / prev_base if prev_base > 0 else 0.0
    delta_accel = delta_ratio - prev_delta_ratio

    last = closes[-1]
    mom5 = last / closes[-6] - 1.0 if len(closes) >= 6 and closes[-6] > 0 else 0.0
    atr_pct = _atr_pct(rows, 20)
    profile = volume_profile(rows)
    vah = float(profile.get('vah') or 0.0)
    val = float(profile.get('val') or 0.0)
    poc = float(profile.get('poc') or 0.0)
    ext_atr = ((last - vah) / last) / max(atr_pct, 0.0015) if last > 0 and vah > 0 and last > vah else 0.0

    strong_buy_flow = taker_buy_ratio >= 0.56 and (delta_ratio >= 0.04 or depth_imbalance >= 0.04)
    negative_flow = taker_buy_ratio < 0.48 and delta_ratio < -0.05 and depth_imbalance < -0.08
    absorption = delta_ratio >= 0.08 and abs(mom5) <= 0.0025 and depth_imbalance >= -0.02
    wy = _wyckoff(rows, strong_buy_flow, taker_buy_ratio)

    return {
        'last': last,
        'taker_buy_ratio': taker_buy_ratio,
        'delta_ratio': delta_ratio,
        'delta_accel': delta_accel,
        'depth_imbalance': depth_imbalance,
        'micro_bias_bps': micro_bias_bps,
        'strong_buy_flow': strong_buy_flow,
        'negative_flow': negative_flow,
        'absorption': absorption,
        'atr_pct': atr_pct,
        'mom5': mom5,
        'poc': poc, 'val': val, 'vah': vah,
        'profile_extension_atr': ext_atr,
        'profile_location': (
            'ABOVE_VAH' if vah > 0 and last > vah else
            'BELOW_VAL' if val > 0 and last < val else
            'ABOVE_POC_IN_VALUE' if poc > 0 and last >= poc else
            'BELOW_POC_IN_VALUE'
        ),
        **wy,
    }


def decide(metrics: dict, payload: dict, regime: str = '') -> dict:
    score = 50.0
    reasons: list[str] = []
    taker = float(metrics.get('taker_buy_ratio') or 0.5)
    delta = float(metrics.get('delta_ratio') or 0.0)
    dacc = float(metrics.get('delta_accel') or 0.0)
    imb = float(metrics.get('depth_imbalance') or 0.0)
    micro = float(metrics.get('micro_bias_bps') or 0.0)
    ext = float(metrics.get('profile_extension_atr') or 0.0)
    loc = str(metrics.get('profile_location') or '')
    strong = bool(metrics.get('strong_buy_flow'))
    negative = bool(metrics.get('negative_flow'))
    phase = str(metrics.get('phase') or 'NONE')

    if taker >= 0.60: score += 10; reasons.append('taker-buy-strong')
    elif taker >= 0.56: score += 7; reasons.append('taker-buy-support')
    elif taker < 0.48: score -= 8; reasons.append('taker-sell-pressure')

    if delta >= 0.10: score += 10; reasons.append('cvd-positive')
    elif delta >= 0.04: score += 6; reasons.append('cvd-support')
    elif delta <= -0.08: score -= 10; reasons.append('cvd-negative')
    if dacc >= 0.06: score += 4; reasons.append('delta-accelerating')
    elif dacc <= -0.08: score -= 4; reasons.append('delta-fading')

    if imb >= 0.10: score += 7; reasons.append('depth-bid-imbalance')
    elif imb >= 0.02: score += 3
    elif imb <= -0.10: score -= 7; reasons.append('depth-ask-imbalance')
    if micro >= 1.0: score += 3
    elif micro <= -1.0: score -= 3

    if loc == 'ABOVE_POC_IN_VALUE': score += 6; reasons.append('profile-acceptance-above-poc')
    elif loc == 'BELOW_POC_IN_VALUE': score += 2
    elif loc == 'ABOVE_VAH' and ext <= 1.20 and strong:
        score += 8; reasons.append('profile-breakout-confirmed')
    elif loc == 'ABOVE_VAH' and ext > 1.60:
        score -= 12; reasons.append('profile-overextended')
    elif loc == 'BELOW_VAL':
        score -= 5; reasons.append('below-value-area')

    if bool(metrics.get('absorption')): score += 6; reasons.append('buy-absorption')
    if phase == 'SPRING': score += 14; reasons.append('wyckoff-spring')
    elif phase == 'SOS': score += 12; reasons.append('wyckoff-sos')
    elif phase == 'LPS': score += 10; reasons.append('wyckoff-lps')
    elif phase == 'UPTHRUST': score -= 30; reasons.append('wyckoff-upthrust')

    score = _clamp(score, 0.0, 100.0)
    regime = str(regime or '').upper()
    required = FLOW_MIN_SCORE
    if regime == 'SIDEWAYS_COMPRESSION':
        required = FLOW_SIDEWAYS_MIN_SCORE
    elif regime in {'WEAK_BEAR', 'PANIC_HIGH_VOL_BEAR'}:
        required = FLOW_WEAK_BEAR_MIN_SCORE

    blockers: list[str] = []
    if negative:
        blockers.append('triple-negative-order-flow')
    if phase == 'UPTHRUST' and not strong:
        blockers.append('wyckoff-upthrust-trap')
    if loc == 'ABOVE_VAH' and ext > 1.80 and not strong:
        blockers.append('volume-profile-chase')
    if score < required:
        blockers.append(f'flow-structure-score<{required:.0f}')

    try:
        entry = float(payload.get('entry') or 0.0)
        stop = float(payload.get('stop') or 0.0)
        stake = float(payload.get('stakeUSDT') or 0.0)
    except Exception:
        entry = stop = stake = 0.0
    risk_pct = (entry - stop) / entry if entry > 0 and 0 < stop < entry else 999.0
    atr_pct = float(metrics.get('atr_pct') or 0.0)
    if risk_pct <= 0 or risk_pct >= 0.03:
        blockers.append('invalid-structural-risk')
    elif atr_pct > 0 and risk_pct < max(0.0035, 0.55 * atr_pct):
        blockers.append('stop-inside-market-noise')

    # Risk sizing is quality-aware, but never increases the upstream stake.
    if score >= 75: quality_mult = 1.0
    elif score >= 60: quality_mult = 0.85
    else: quality_mult = 0.70
    adjusted = stake * quality_mult if stake > 0 else 0.0
    if risk_pct > 0 and risk_pct < 1:
        adjusted = min(adjusted, FLOW_MAX_STOP_RISK_USDT / risk_pct)
    adjusted = math.floor(max(0.0, adjusted) * 100.0) / 100.0
    if stake >= FLOW_MIN_EXEC_STAKE_USDT and adjusted < FLOW_MIN_EXEC_STAKE_USDT:
        blockers.append('risk-sized-below-min-executable-stake')
    if stake > 0:
        adjusted = min(stake, max(FLOW_MIN_EXEC_STAKE_USDT, adjusted))

    return {
        'passed': not blockers,
        'status': 'FLOW_STRUCTURE_PASS' if not blockers else 'FLOW_STRUCTURE_REJECT',
        'reason': 'ok' if not blockers else '|'.join(blockers),
        'quality_score': round(score, 2),
        'required_score': required,
        'stake_multiplier': quality_mult,
        'adjusted_stake_usdt': round(adjusted, 2),
        'risk_pct': risk_pct,
        'reasons': reasons,
        'metrics': metrics,
    }


def evaluate(symbol: str, payload: dict, regime: str = '') -> dict:
    try:
        metrics = collect_metrics(symbol)
        return decide(metrics, payload, regime)
    except Exception as exc:
        return {
            'passed': not FLOW_FAIL_CLOSED,
            'status': 'FLOW_DATA_UNAVAILABLE',
            'reason': f'{type(exc).__name__}:{str(exc)[:160]}',
            'quality_score': None,
            'required_score': None,
            'adjusted_stake_usdt': float(payload.get('stakeUSDT') or 0.0),
            'metrics': {},
        }
