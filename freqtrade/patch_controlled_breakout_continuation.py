from pathlib import Path

p = Path('/freqtrade/fast_entry_engine.py')
s = p.read_text(encoding='utf-8')

# The normal ignition lane was intentionally ultra-early: only the first 0.25%
# above resistance, <=0.60% 5m momentum and RSI around <=68.  That is safe, but
# it leaves a real coverage hole: a liquid coin can begin a clean 0.4-1.0% 5m
# continuation and be hard-zeroed before MID (which starts at +5% over 24h)
# becomes eligible.  Fill ONLY that gap.  This is not a generic chase mode.

old_const = "IGNITION_MAX_EXTENSION = float(os.getenv('FAST_IGNITION_MAX_EXTENSION', '0.0025'))\n"
new_const = (
    "IGNITION_MAX_EXTENSION = float(os.getenv('FAST_IGNITION_MAX_EXTENSION', '0.0060'))\n"
    "IGNITION_FIRST_BREAK_MAX_EXTENSION = float(os.getenv('FAST_IGNITION_FIRST_BREAK_MAX_EXTENSION', '0.0025'))\n"
    "IGNITION_CONT_MIN_VOLUME = float(os.getenv('FAST_IGNITION_CONT_MIN_VOLUME', '1.20'))\n"
    "IGNITION_CONT_MIN_TAKER = float(os.getenv('FAST_IGNITION_CONT_MIN_TAKER', '0.62'))\n"
    "IGNITION_CONT_MAX_RSI = float(os.getenv('FAST_IGNITION_CONT_MAX_RSI', '74'))\n"
    "IGNITION_CONT_MAX_MOM5 = float(os.getenv('FAST_IGNITION_CONT_MAX_MOM5', '0.012'))\n"
    "IGNITION_CONT_MAX_MOM15 = float(os.getenv('FAST_IGNITION_CONT_MAX_MOM15', '0.022'))\n"
    "IGNITION_CONT_MAX_SPREAD = float(os.getenv('FAST_IGNITION_CONT_MAX_SPREAD', '0.08'))\n"
    "IGNITION_CONT_MAX_WICK = float(os.getenv('FAST_IGNITION_CONT_MAX_WICK', '1.80'))\n"
)
if 'IGNITION_CONT_MIN_VOLUME' not in s:
    if old_const not in s:
        raise SystemExit('controlled-continuation: ignition constant marker missing')
    s = s.replace(old_const, new_const, 1)

old_anti = '''    live_extension = float(m.get('live_extension', -9.0))
    fresh_ignition_zone = 0.0 <= live_extension <= IGNITION_MAX_EXTENSION

    # The old engine rejected the breakout itself. Now we allow only the very
    # first live break, while still rejecting a move that has already run away.
    momentum_spent = (
        live_extension > IGNITION_MAX_EXTENSION or
        ((m['last'] >= m['breakout_level'] or m['distance_to_breakout'] < MIN_BREAKOUT_DISTANCE) and not fresh_ignition_zone) or
        m['mom5'] > MAX_MOM5 or
        m['mom15'] > MAX_MOM15 or
        m['mom30'] > MAX_MOM30 or
        m['rsi'] > IGNITION_MAX_RSI
    )
    if momentum_spent:
        return 0.0, ['momentum-already-spent']

'''
new_anti = '''    live_extension = float(m.get('live_extension', -9.0))
    first_break_zone = 0.0 <= live_extension <= IGNITION_FIRST_BREAK_MAX_EXTENSION
    continuation_zone = IGNITION_FIRST_BREAK_MAX_EXTENSION < live_extension <= IGNITION_MAX_EXTENSION
    strong_continuation = (
        continuation_zone
        and m['volume_ratio'] >= IGNITION_CONT_MIN_VOLUME
        and m['taker_buy_ratio'] >= IGNITION_CONT_MIN_TAKER
        and m['spread_pct'] <= IGNITION_CONT_MAX_SPREAD
        and m['wick_ratio'] <= IGNITION_CONT_MAX_WICK
        and m['rsi'] <= IGNITION_CONT_MAX_RSI
        and m['mom5'] <= IGNITION_CONT_MAX_MOM5
        and m['mom15'] <= IGNITION_CONT_MAX_MOM15
    )
    fresh_ignition_zone = first_break_zone or strong_continuation
    mom5_cap = IGNITION_CONT_MAX_MOM5 if strong_continuation else MAX_MOM5
    mom15_cap = IGNITION_CONT_MAX_MOM15 if strong_continuation else MAX_MOM15
    rsi_cap = IGNITION_CONT_MAX_RSI if strong_continuation else IGNITION_MAX_RSI

    # Permit a small, flow-confirmed continuation after the first break, but
    # still hard-reject stale/vertical/overheated moves.
    momentum_spent = (
        live_extension > IGNITION_MAX_EXTENSION or
        ((m['last'] >= m['breakout_level'] or m['distance_to_breakout'] < MIN_BREAKOUT_DISTANCE) and not fresh_ignition_zone) or
        m['mom5'] > mom5_cap or
        m['mom15'] > mom15_cap or
        m['mom30'] > MAX_MOM30 or
        m['rsi'] > rsi_cap or
        (continuation_zone and not strong_continuation)
    )
    if momentum_spent:
        return 0.0, ['momentum-already-spent']

'''
if 'strong_continuation = (' not in s:
    if old_anti not in s:
        raise SystemExit('controlled-continuation: anti-chase marker missing')
    s = s.replace(old_anti, new_anti, 1)

old_bonus = "    if fresh_ignition_zone:\n        score += 18; reasons.append('fresh-breakout')\n"
new_bonus = (
    "    if fresh_ignition_zone:\n"
    "        score += 18; reasons.append('fresh-breakout')\n"
    "    if strong_continuation:\n"
    "        score += 16; reasons.append('controlled-breakout-continuation')\n"
)
if "reasons.append('controlled-breakout-continuation')" not in s:
    if old_bonus not in s:
        raise SystemExit('controlled-continuation: score bonus marker missing')
    s = s.replace(old_bonus, new_bonus, 1)

old_micro = '''def _ignition_aware_micro_gate(m: dict) -> tuple[bool, str]:
    ext = float(m.get('live_extension', -9.0))
    if 0.0 <= ext <= IGNITION_MAX_EXTENSION:
        if m['volume_ratio'] < IGNITION_MIN_VOLUME and not m['volume_accel']:
            return False, 'volume-too-weak'
        if m['taker_buy_ratio'] < IGNITION_MIN_TAKER:
            return False, 'buy-pressure-too-weak'
        if m['spread_pct'] > MAX_SPREAD_PCT:
            return False, 'spread-too-wide'
        if m['compression_ratio'] > 1.15:
            return False, 'no-compression'
        if m['wick_ratio'] > 3.0:
            return False, 'rejection-wick'
        return True, 'fresh-ignition-micro-ok'
    return _ORIGINAL_MICRO_GATE(m)
'''
new_micro = '''def _ignition_aware_micro_gate(m: dict) -> tuple[bool, str]:
    ext = float(m.get('live_extension', -9.0))
    if 0.0 <= ext <= IGNITION_FIRST_BREAK_MAX_EXTENSION:
        if m['volume_ratio'] < IGNITION_MIN_VOLUME and not m['volume_accel']:
            return False, 'volume-too-weak'
        if m['taker_buy_ratio'] < IGNITION_MIN_TAKER:
            return False, 'buy-pressure-too-weak'
        if m['spread_pct'] > MAX_SPREAD_PCT:
            return False, 'spread-too-wide'
        if m['compression_ratio'] > 1.15:
            return False, 'no-compression'
        if m['wick_ratio'] > 3.0:
            return False, 'rejection-wick'
        return True, 'fresh-ignition-micro-ok'
    if IGNITION_FIRST_BREAK_MAX_EXTENSION < ext <= IGNITION_MAX_EXTENSION:
        if m['volume_ratio'] < IGNITION_CONT_MIN_VOLUME:
            return False, 'continuation-volume-too-weak'
        if m['taker_buy_ratio'] < IGNITION_CONT_MIN_TAKER:
            return False, 'continuation-buy-pressure-too-weak'
        if m['spread_pct'] > IGNITION_CONT_MAX_SPREAD:
            return False, 'continuation-spread-too-wide'
        if m['wick_ratio'] > IGNITION_CONT_MAX_WICK:
            return False, 'continuation-rejection-wick'
        if m['rsi'] > IGNITION_CONT_MAX_RSI:
            return False, 'continuation-rsi-overheated'
        return True, 'controlled-continuation-micro-ok'
    return _ORIGINAL_MICRO_GATE(m)
'''
if 'controlled-continuation-micro-ok' not in s:
    if old_micro not in s:
        raise SystemExit('controlled-continuation: micro-gate marker missing')
    s = s.replace(old_micro, new_micro, 1)

old_final = '''    # GREEN trigger: send only on the first live break above resistance, not before
    # and not after the move has already extended.
    live_extension = float(m.get('live_extension', -9.0))
    ignition_pressure = (
        m['taker_buy_ratio'] >= IGNITION_MIN_TAKER
        and (m['volume_ratio'] >= IGNITION_MIN_VOLUME or m['volume_accel'])
    )
    fresh_ignition = (
        0.0 <= live_extension <= IGNITION_MAX_EXTENSION
        and ignition_pressure
        and m['ema9'] > m['ema21']
        and m['rsi'] <= IGNITION_MAX_RSI
        and m['spread_pct'] <= MAX_SPREAD_PCT
        and m['mom5'] <= MAX_MOM5
        and m['mom15'] <= MAX_MOM15
    )
    if not fresh_ignition:
        return False

'''
new_final = '''    # GREEN trigger: first break OR a very small continuation confirmed by
    # stronger volume/taker flow.  Anything beyond 0.60% above the breakout is
    # still considered chase and is rejected.
    live_extension = float(m.get('live_extension', -9.0))
    ignition_pressure = (
        m['taker_buy_ratio'] >= IGNITION_MIN_TAKER
        and (m['volume_ratio'] >= IGNITION_MIN_VOLUME or m['volume_accel'])
    )
    first_break = (
        0.0 <= live_extension <= IGNITION_FIRST_BREAK_MAX_EXTENSION
        and ignition_pressure
        and m['ema9'] > m['ema21']
        and m['rsi'] <= IGNITION_MAX_RSI
        and m['spread_pct'] <= MAX_SPREAD_PCT
        and m['mom5'] <= MAX_MOM5
        and m['mom15'] <= MAX_MOM15
    )
    controlled_continuation = (
        IGNITION_FIRST_BREAK_MAX_EXTENSION < live_extension <= IGNITION_MAX_EXTENSION
        and m['volume_ratio'] >= IGNITION_CONT_MIN_VOLUME
        and m['taker_buy_ratio'] >= IGNITION_CONT_MIN_TAKER
        and m['ema9'] > m['ema21']
        and m['rsi'] <= IGNITION_CONT_MAX_RSI
        and m['spread_pct'] <= IGNITION_CONT_MAX_SPREAD
        and m['wick_ratio'] <= IGNITION_CONT_MAX_WICK
        and m['mom5'] <= IGNITION_CONT_MAX_MOM5
        and m['mom15'] <= IGNITION_CONT_MAX_MOM15
    )
    if not (first_break or controlled_continuation):
        return False

'''
if 'controlled_continuation = (' not in s:
    if old_final not in s:
        raise SystemExit('controlled-continuation: final-gate marker missing')
    s = s.replace(old_final, new_final, 1)

# Keep the existing persistent 2-hit confirmation, Spot Sniper, live R:R,
# order-flow/volume-profile/Wyckoff, OCO and all reconciled risk circuits.  This
# patch only fills the short-term 0.25-0.60% post-breakout coverage gap.
for marker in [
    'IGNITION_CONT_MIN_VOLUME',
    'controlled-breakout-continuation',
    'controlled-continuation-micro-ok',
    'controlled_continuation = (',
]:
    if marker not in s:
        raise SystemExit(f'controlled-continuation: missing marker {marker}')

compile(s, str(p), 'exec')
p.write_text(s, encoding='utf-8')
print('[controlled-breakout-continuation] OK post-breakout extension<=0.60% with vol>=1.20 taker>=62% RSI<=74; existing downstream safety unchanged')
