from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text()

# Scan faster so a fresh breakout is seen close to the moment it starts.
s = s.replace(
    "SCAN_INTERVAL_SEC = int(os.getenv('FAST_SCAN_INTERVAL_SEC', '60'))",
    "SCAN_INTERVAL_SEC = int(os.getenv('FAST_SCAN_INTERVAL_SEC', '20'))",
    1,
)

const_marker = "MAX_RSI = float(os.getenv('FAST_MAX_RSI', '66'))\n"
const_insert = const_marker + (
    "IGNITION_MAX_EXTENSION = float(os.getenv('FAST_IGNITION_MAX_EXTENSION', '0.0025'))\n"
    "IGNITION_MIN_TAKER = float(os.getenv('FAST_MIN_TAKER_BUY_RATIO', '0.58'))\n"
    "IGNITION_MIN_VOLUME = float(os.getenv('FAST_MIN_VOLUME_RATIO', '0.95'))\n"
    "IGNITION_MAX_RSI = float(os.getenv('FAST_IGNITION_MAX_RSI', '68'))\n"
)
if 'IGNITION_MAX_EXTENSION' not in s:
    if const_marker not in s:
        raise SystemExit('momentum ignition patch failed: constants marker missing')
    s = s.replace(const_marker, const_insert, 1)

book_marker = "    top_book_ratio = bid_qty / ask_qty if ask_qty > 0 else 0.0\n\n"
book_insert = book_marker + (
    "    # Live ask is used for the trigger so we do not wait for the 1m candle to close.\n"
    "    live_price = ask if ask > 0 else mid\n"
    "    live_extension = live_price / breakout_level - 1.0 if breakout_level > 0 else 9.0\n\n"
)
if "'live_extension': live_extension" not in s:
    if book_marker not in s:
        raise SystemExit('momentum ignition patch failed: book marker missing')
    s = s.replace(book_marker, book_insert, 1)
    return_marker = "        'last': last,\n"
    if return_marker not in s:
        raise SystemExit('momentum ignition patch failed: metrics return marker missing')
    s = s.replace(
        return_marker,
        return_marker + "        'live_price': live_price,\n        'live_extension': live_extension,\n",
        1,
    )

old_anti = '''    # Hard anti-chase: a move already in progress is not a pre-momentum setup.\n    momentum_spent = (\n        m['last'] >= m['breakout_level'] or\n        m['distance_to_breakout'] < MIN_BREAKOUT_DISTANCE or\n        m['mom5'] > MAX_MOM5 or\n        m['mom15'] > MAX_MOM15 or\n        m['mom30'] > MAX_MOM30 or\n        m['rsi'] > MAX_RSI\n    )\n    if momentum_spent:\n        return 0.0, ['momentum-already-spent']\n\n'''
new_anti = '''    live_extension = float(m.get('live_extension', -9.0))\n    fresh_ignition_zone = 0.0 <= live_extension <= IGNITION_MAX_EXTENSION\n\n    # The old engine rejected the breakout itself. Now we allow only the very\n    # first live break, while still rejecting a move that has already run away.\n    momentum_spent = (\n        live_extension > IGNITION_MAX_EXTENSION or\n        ((m['last'] >= m['breakout_level'] or m['distance_to_breakout'] < MIN_BREAKOUT_DISTANCE) and not fresh_ignition_zone) or\n        m['mom5'] > MAX_MOM5 or\n        m['mom15'] > MAX_MOM15 or\n        m['mom30'] > MAX_MOM30 or\n        m['rsi'] > IGNITION_MAX_RSI\n    )\n    if momentum_spent:\n        return 0.0, ['momentum-already-spent']\n\n'''
if 'fresh_ignition_zone = ' not in s:
    if old_anti not in s:
        raise SystemExit('momentum ignition patch failed: anti-chase marker missing')
    s = s.replace(old_anti, new_anti, 1)

trend_marker = "    if m['ema9'] > m['ema21'] and m['last'] >= m['ema9'] * 0.998:\n        score += 14; reasons.append('early-trend')\n"
if "reasons.append('fresh-breakout')" not in s:
    if trend_marker not in s:
        raise SystemExit('momentum ignition patch failed: score marker missing')
    s = s.replace(
        trend_marker,
        trend_marker + "    if fresh_ignition_zone:\n        score += 18; reasons.append('fresh-breakout')\n",
        1,
    )

# Make the existing quality module accept the tiny live breakout window while
# keeping its volume, taker, spread, compression and wick protections.
quality_marker = "def score_setup(m: dict) -> tuple[float, list[str]]:\n"
quality_helper = '''_ORIGINAL_MICRO_GATE = entry_quality.micro_gate\n\ndef _ignition_aware_micro_gate(m: dict) -> tuple[bool, str]:\n    ext = float(m.get('live_extension', -9.0))\n    if 0.0 <= ext <= IGNITION_MAX_EXTENSION:\n        if m['volume_ratio'] < IGNITION_MIN_VOLUME and not m['volume_accel']:\n            return False, 'volume-too-weak'\n        if m['taker_buy_ratio'] < IGNITION_MIN_TAKER:\n            return False, 'buy-pressure-too-weak'\n        if m['spread_pct'] > MAX_SPREAD_PCT:\n            return False, 'spread-too-wide'\n        if m['compression_ratio'] > 1.15:\n            return False, 'no-compression'\n        if m['wick_ratio'] > 3.0:\n            return False, 'rejection-wick'\n        return True, 'fresh-ignition-micro-ok'\n    return _ORIGINAL_MICRO_GATE(m)\n\nentry_quality.micro_gate = _ignition_aware_micro_gate\n\n\n'''
if '_ignition_aware_micro_gate' not in s:
    if quality_marker not in s:
        raise SystemExit('momentum ignition patch failed: quality helper marker missing')
    s = s.replace(quality_marker, quality_helper + quality_marker, 1)

old_final = '''    # Final fail-closed anti-chase check immediately before creating the confirm signal.\n    if not (MIN_BREAKOUT_DISTANCE <= m['distance_to_breakout'] <= MAX_BREAKOUT_DISTANCE):\n        return False\n    if m['mom5'] > MAX_MOM5 or m['mom15'] > MAX_MOM15 or m['rsi'] > MAX_RSI:\n        return False\n\n'''
new_final = '''    # GREEN trigger: send only on the first live break above resistance, not before\n    # and not after the move has already extended.\n    live_extension = float(m.get('live_extension', -9.0))\n    ignition_pressure = (\n        m['taker_buy_ratio'] >= IGNITION_MIN_TAKER\n        and (m['volume_ratio'] >= IGNITION_MIN_VOLUME or m['volume_accel'])\n    )\n    fresh_ignition = (\n        0.0 <= live_extension <= IGNITION_MAX_EXTENSION\n        and ignition_pressure\n        and m['ema9'] > m['ema21']\n        and m['rsi'] <= IGNITION_MAX_RSI\n        and m['spread_pct'] <= MAX_SPREAD_PCT\n        and m['mom5'] <= MAX_MOM5\n        and m['mom15'] <= MAX_MOM15\n    )\n    if not fresh_ignition:\n        return False\n\n'''
if '# GREEN trigger:' not in s:
    if old_final not in s:
        raise SystemExit('momentum ignition patch failed: final gate marker missing')
    s = s.replace(old_final, new_final, 1)

old_targets = "    tp_pct = clamp(max(0.009, m['atr_pct'] * 5.0), 0.009, 0.014)\n    sl_pct = clamp(tp_pct / 1.55, 0.0055, 0.0090)\n    entry = m['last']\n"
new_targets = "    tp_pct = clamp(max(0.012, m['atr_pct'] * 6.0), 0.012, 0.020)\n    sl_pct = clamp(tp_pct / 2.0, 0.0055, 0.0090)\n    entry = m['live_price']\n"
if "m['atr_pct'] * 6.0" not in s:
    if old_targets not in s:
        raise SystemExit('momentum ignition patch failed: target marker missing')
    s = s.replace(old_targets, new_targets, 1)

old_tag = "        f\"PREMOMENTUM|score={score:.0f}|dist={m['distance_to_breakout']*100:.2f}%|\"\n"
new_tag = "        f\"IGNITION_GREEN|score={score:.0f}|ext={m['live_extension']*100:.3f}%|dist={m['distance_to_breakout']*100:.2f}%|\"\n"
if 'IGNITION_GREEN|score=' not in s:
    if old_tag not in s:
        raise SystemExit('momentum ignition patch failed: strategy tag marker missing')
    s = s.replace(old_tag, new_tag, 1)

s = s.replace("print(f'[pre-momentum] one-tap ready", "print(f'[momentum-ignition] one-tap ready", 1)
s = s.replace("ONLINE mode=PRE_MOMENTUM", "ONLINE mode=MOMENTUM_IGNITION", 1)
s = s.replace("f\"[pre-score] {symbol}", "f\"[ignition-score] {symbol}", 1)

for required in [
    "ONLINE mode=MOMENTUM_IGNITION",
    "IGNITION_GREEN|score=",
    "# GREEN trigger:",
    "'live_price': live_price",
    "entry = m['live_price']",
    "_ignition_aware_micro_gate",
]:
    if required not in s:
        raise SystemExit(f'momentum ignition patch failed: missing {required}')

path.write_text(s)
print('[momentum-ignition-patch] OK live first-break trigger + 20s scan + anti-chase + 1.2-2.0% target')
