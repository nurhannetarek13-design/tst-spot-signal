from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Research-only barrier construction for SCANNED candidates. This does not change
# live entry eligibility, live sizing, live TP/SL, or execution behavior. It only
# gives the forward outcome engine a point-in-time target/stop pair so it can
# learn P(TP-before-SL) instead of collecting unlabeled MFE/MAE rows forever.
helper_marker = '\ndef validate_and_resolve_telegram_chat() -> bool:\n'
helper = r'''

def _research_barriers(lane: str, price: float, score: float, metrics: dict) -> dict:
    try:
        px = float(price or 0.0)
    except Exception:
        px = 0.0
    if px <= 0:
        return {'research_barrier_version': 'POINT_IN_TIME_V1_INVALID'}

    def _f(v, default=0.0):
        try:
            x = float(v)
            return x if x == x else float(default)
        except Exception:
            return float(default)

    def _clip(v, lo, hi):
        return min(float(hi), max(float(lo), float(v)))

    lane = str(lane or 'NORMAL').upper()
    atr = _f(metrics.get('atr_pct'), _f(metrics.get('atr'), 0.0020))
    taker = _f(metrics.get('taker_buy_ratio'), _f(metrics.get('taker'), 0.58))
    volx = _f(metrics.get('volume_ratio'), _f(metrics.get('volx'), 1.0))

    if lane == 'NORMAL':
        # Mirrors the deployed Growth Mode policy before exchange tick normalization.
        base_tp = max(0.015, atr * 6.0)
        score_strength = _clip((_f(score) - 90.0) / 10.0, 0.0, 1.0)
        pressure_strength = _clip((taker - 0.58) / 0.10, 0.0, 1.0)
        volume_strength = _clip((volx - 1.0) / 1.0, 0.0, 1.0)
        tp_pct = _clip(base_tp + 0.008 * score_strength + 0.006 * pressure_strength + 0.005 * volume_strength, 0.015, 0.035)
        sl_pct = _clip(tp_pct / 2.4, 0.0055, 0.0090)
        version = 'POINT_IN_TIME_V1_NORMAL_GROWTH'
    elif lane == 'EXPLOSIVE':
        sl_pct = _clip(max(0.010, atr * 3.2), 0.010, 0.022)
        tp_pct = _clip(sl_pct * 1.65, 0.017, 0.038)
        version = 'POINT_IN_TIME_V1_EXPLOSIVE'
    elif lane == 'EXTREME':
        sl_pct = _clip(max(0.012, atr * 3.6), 0.012, 0.026)
        tp_pct = _clip(sl_pct * 1.80, 0.022, 0.045)
        version = 'POINT_IN_TIME_V1_EXTREME'
    else:  # MID continuation: deliberately conservative research envelope.
        sl_pct = _clip(max(0.0085, atr * 3.0), 0.0085, 0.016)
        tp_pct = _clip(sl_pct * 1.90, 0.016, 0.032)
        version = 'POINT_IN_TIME_V1_MID'

    return {
        'target': px * (1.0 + tp_pct),
        'stop': px * (1.0 - sl_pct),
        'risk_pct': sl_pct,
        'reward_pct': tp_pct,
        'atr_pct': atr,
        'research_barrier_version': version,
    }

'''
if 'def _research_barriers(' not in s:
    if helper_marker not in s:
        raise SystemExit('research-barriers: helper insertion marker missing')
    s = s.replace(helper_marker, helper + helper_marker, 1)

old_new = [
    (
        "                    _record_candidate(symbol, 'NORMAL', score, float(m.get('live_price') or m.get('last') or 0), 'SCANNED', 'score-snapshot', volume_ratio=m.get('volume_ratio'), taker_buy_ratio=m.get('taker_buy_ratio'), spread_pct=m.get('spread_pct'), rsi=m.get('rsi'), change24=change, volume24=volume)\n",
        "                    _research_px = float(m.get('live_price') or m.get('last') or 0)\n                    _research_b = _research_barriers('NORMAL', _research_px, score, m)\n                    _record_candidate(symbol, 'NORMAL', score, _research_px, 'SCANNED', 'score-snapshot', volume_ratio=m.get('volume_ratio'), taker_buy_ratio=m.get('taker_buy_ratio'), spread_pct=m.get('spread_pct'), rsi=m.get('rsi'), change24=change, volume24=volume, **_research_b)\n",
    ),
    (
        "                    _record_candidate(es, 'EXPLOSIVE', escore, float(em.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=em.get('pullback'), reclaimed=em.get('reclaimed'), volume_ratio=em.get('volx'), taker_buy_ratio=em.get('taker'), spread_pct=em.get('spread'), rsi=em.get('rsi'), change24=ech, volume24=ev)\n",
        "                    _research_px = float(em.get('live') or 0)\n                    _research_b = _research_barriers('EXPLOSIVE', _research_px, escore, em)\n                    _record_candidate(es, 'EXPLOSIVE', escore, _research_px, 'SCANNED', 'score-snapshot', pullback=em.get('pullback'), reclaimed=em.get('reclaimed'), volume_ratio=em.get('volx'), taker_buy_ratio=em.get('taker'), spread_pct=em.get('spread'), rsi=em.get('rsi'), change24=ech, volume24=ev, **_research_b)\n",
    ),
    (
        "                    _record_candidate(xs, 'EXTREME', xscore, float(xm.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=xm.get('pullback'), reclaimed=xm.get('reclaimed'), volume_ratio=xm.get('volx'), taker_buy_ratio=xm.get('taker'), spread_pct=xm.get('spread'), rsi=xm.get('rsi'), change24=xch, volume24=xv)\n",
        "                    _research_px = float(xm.get('live') or 0)\n                    _research_b = _research_barriers('EXTREME', _research_px, xscore, xm)\n                    _record_candidate(xs, 'EXTREME', xscore, _research_px, 'SCANNED', 'score-snapshot', pullback=xm.get('pullback'), reclaimed=xm.get('reclaimed'), volume_ratio=xm.get('volx'), taker_buy_ratio=xm.get('taker'), spread_pct=xm.get('spread'), rsi=xm.get('rsi'), change24=xch, volume24=xv, **_research_b)\n",
    ),
    (
        "                    _record_candidate(ms, 'MID', mscore, float(mm.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=mm.get('pullback'), reclaimed=mm.get('reclaimed'), volume_ratio=mm.get('volx'), taker_buy_ratio=mm.get('taker'), spread_pct=mm.get('spread'), rsi=mm.get('rsi'), change24=mch, volume24=mv)\n",
        "                    _research_px = float(mm.get('live') or 0)\n                    _research_b = _research_barriers('MID', _research_px, mscore, mm)\n                    _record_candidate(ms, 'MID', mscore, _research_px, 'SCANNED', 'score-snapshot', pullback=mm.get('pullback'), reclaimed=mm.get('reclaimed'), volume_ratio=mm.get('volx'), taker_buy_ratio=mm.get('taker'), spread_pct=mm.get('spread'), rsi=mm.get('rsi'), change24=mch, volume24=mv, **_research_b)\n",
    ),
]

patched = 0
for old, new in old_new:
    if new in s:
        patched += 1
        continue
    if old in s:
        s = s.replace(old, new, 1)
        patched += 1

# NORMAL is always present. Continuation lanes are build-patched and should all
# be present in production; fail build if any expected lane missed instrumentation.
if patched != 4:
    raise SystemExit(f'research-barriers: expected 4 telemetry lanes, patched={patched}')

required = [
    'def _research_barriers(',
    "'research_barrier_version': version",
    "_research_barriers('NORMAL'",
    "_research_barriers('EXPLOSIVE'",
    "_research_barriers('MID'",
    "_research_barriers('EXTREME'",
]
for marker in required:
    if marker not in s:
        raise SystemExit(f'research-barriers: required marker missing: {marker}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[research-barriers-v1] OK all 4 lanes emit point-in-time TP/SL labels for forward EV research')
