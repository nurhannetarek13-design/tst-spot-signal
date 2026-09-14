from pathlib import Path

# Spot Sniper whipsaw + profit-giveback protection.
#
# Goals:
# 1) Stop one-scan momentum spikes becoming immediate BUYs.
# 2) Keep the initial hard OCO outside ordinary 1m noise without removing it.
# 3) Do not tighten a fresh position just because pre-entry candles had a high.
# 4) Once a trade earns meaningful open profit, ratchet the OCO fast enough that
#    a red reversal cannot give most of that move back.
#
# This patch never removes the exchange stop and never raises the configured
# dollars-at-risk ceiling. It only changes entry persistence and OCO placement.

fast_path = Path('/freqtrade/fast_entry_engine.py')
s = fast_path.read_text(encoding='utf-8')

# ---------------------------------------------------------------------------
# Entry persistence + noise-aware initial stop.
# ---------------------------------------------------------------------------
const_marker = "WATCH_TTL_SEC = int(os.getenv('FAST_WATCH_TTL_SEC', '300'))\n"
const_block = const_marker + (
    "DIRECT_CONFIRM_HITS = max(2, int(os.getenv('FAST_DIRECT_CONFIRM_HITS', '2')))\n"
    "DIRECT_CONFIRM_MIN_SEC = max(30, int(os.getenv('FAST_DIRECT_CONFIRM_MIN_SEC', '45')))\n"
    "DIRECT_CONFIRM_MAX_GAP_SEC = max(DIRECT_CONFIRM_MIN_SEC + 15, int(os.getenv('FAST_DIRECT_CONFIRM_MAX_GAP_SEC', '150')))\n"
    "WHIPSAW_MIN_STOP_PCT = max(0.0065, float(os.getenv('FAST_WHIPSAW_MIN_STOP_PCT', '0.0075')))\n"
    "WHIPSAW_MAX_STOP_PCT = min(0.0150, max(WHIPSAW_MIN_STOP_PCT, float(os.getenv('FAST_WHIPSAW_MAX_STOP_PCT', '0.0110'))))\n"
    "WHIPSAW_ATR_MULT = max(1.5, float(os.getenv('FAST_WHIPSAW_ATR_MULT', '2.4')))\n"
    "WHIPSAW_MIN_RR = max(1.30, float(os.getenv('FAST_WHIPSAW_MIN_RR', '1.45')))\n"
)
if 'DIRECT_CONFIRM_HITS =' not in s:
    if const_marker not in s:
        raise SystemExit('whipsaw patch failed: WATCH_TTL_SEC marker missing')
    s = s.replace(const_marker, const_block, 1)

state_marker = "adaptive_watch: dict[str, dict] = {}\n"
state_block = state_marker + "direct_confirm_state: dict[str, dict] = {}\n"
if 'direct_confirm_state: dict[str, dict]' not in s:
    if state_marker not in s:
        raise SystemExit('whipsaw patch failed: adaptive watch state marker missing')
    s = s.replace(state_marker, state_block, 1)

helper_marker = "\ndef _watch_live_price(m: dict) -> float:\n"
helpers = r'''

def _whipsaw_stop_pct(proposed: float, m: dict) -> float:
    """Keep the hard stop beyond ordinary 1m noise, within a strict cap."""
    try:
        atr = float(m.get('atr_pct') or m.get('atr') or 0.0)
    except Exception:
        atr = 0.0
    floor = max(WHIPSAW_MIN_STOP_PCT, atr * WHIPSAW_ATR_MULT)
    return clamp(max(float(proposed), floor), WHIPSAW_MIN_STOP_PCT, WHIPSAW_MAX_STOP_PCT)


def _direct_confirmation_ready(symbol: str, score: float, m: dict) -> bool:
    """Require persistent direct-grade pressure across at least two scans."""
    now = time.time()
    price = _watch_live_price(m)
    row = direct_confirm_state.get(symbol)
    if row is None or now - float(row.get('last_at') or 0.0) > DIRECT_CONFIRM_MAX_GAP_SEC:
        direct_confirm_state[symbol] = {
            'first_at': now, 'last_at': now, 'hits': 1,
            'first_score': float(score), 'last_score': float(score),
            'first_price': price,
        }
        print(f'[direct-confirm] ARM {symbol} score={score:.0f} hits=1/{DIRECT_CONFIRM_HITS} min_age={DIRECT_CONFIRM_MIN_SEC}s')
        return False

    # Multiple lane touches in the same scanner moment do not count as separate
    # confirmations.
    if now - float(row.get('last_at') or 0.0) < 20:
        return False

    row['last_at'] = now
    row['last_score'] = float(score)
    row['hits'] = int(row.get('hits') or 1) + 1
    age = now - float(row.get('first_at') or now)
    if int(row['hits']) >= DIRECT_CONFIRM_HITS and age >= DIRECT_CONFIRM_MIN_SEC:
        direct_confirm_state.pop(symbol, None)
        print(f'[direct-confirm] PASS {symbol} score={score:.0f} hits={row["hits"]} age={age:.0f}s -> final gates')
        return True

    print(f'[direct-confirm] HOLD {symbol} score={score:.0f} hits={row["hits"]}/{DIRECT_CONFIRM_HITS} age={age:.0f}s')
    return False

'''
if 'def _whipsaw_stop_pct(' not in s:
    if helper_marker not in s:
        raise SystemExit('whipsaw patch failed: watch helper insertion marker missing')
    s = s.replace(helper_marker, helpers + helper_marker, 1)

loop_header_old = "            for score, symbol, change, volume, _ in ranked[:15]:\n"
loop_header_new = "            for score, symbol, change, volume, m in ranked[:15]:\n"
if loop_header_old in s:
    s = s.replace(loop_header_old, loop_header_new, 1)
elif loop_header_new not in s:
    raise SystemExit('whipsaw patch failed: normal direct loop header missing')

signal_call_old = "                try:\n                    if maybe_signal(symbol, change, volume):\n                        break\n"
signal_call_new = (
    "                try:\n"
    "                    if not _direct_confirmation_ready(symbol, score, m):\n"
    "                        continue\n"
    "                    if maybe_signal(symbol, change, volume):\n"
    "                        break\n"
)
if 'if not _direct_confirmation_ready(symbol, score, m):' not in s:
    if signal_call_old not in s:
        raise SystemExit('whipsaw patch failed: normal maybe_signal call marker missing')
    s = s.replace(signal_call_old, signal_call_new, 1)

stop_old = (
    "    tp_pct = clamp(max(0.009, m['atr_pct'] * 5.0), 0.009, 0.014)\n"
    "    sl_pct = clamp(tp_pct / 1.55, 0.0055, 0.0090)\n"
)
stop_new = stop_old + (
    "    sl_pct = _whipsaw_stop_pct(sl_pct, m)\n"
    "    tp_pct = clamp(max(tp_pct, sl_pct * WHIPSAW_MIN_RR), 0.0100, 0.0180)\n"
)
if 'sl_pct = _whipsaw_stop_pct(sl_pct, m)' not in s:
    if stop_old not in s:
        raise SystemExit('whipsaw patch failed: normal TP/SL marker missing')
    s = s.replace(stop_old, stop_new, 1)

required_fast = [
    'DIRECT_CONFIRM_HITS =', 'DIRECT_CONFIRM_MIN_SEC =',
    'WHIPSAW_MIN_STOP_PCT =', 'WHIPSAW_ATR_MULT =', 'WHIPSAW_MIN_RR =',
    'def _direct_confirmation_ready(', 'def _whipsaw_stop_pct(',
    'if not _direct_confirmation_ready(symbol, score, m):',
    'sl_pct = _whipsaw_stop_pct(sl_pct, m)',
]
for item in required_fast:
    if item not in s:
        raise SystemExit(f'whipsaw patch failed: missing fast marker {item}')
compile(s, str(fast_path), 'exec')
fast_path.write_text(s, encoding='utf-8')

# ---------------------------------------------------------------------------
# Dynamic exit: correct peak tracking + profit giveback protection.
# ---------------------------------------------------------------------------
dex_path = Path('/freqtrade/dynamic_exit_manager.py')
d = dex_path.read_text(encoding='utf-8')

ratchet_const = "MIN_RATCHET_GAP_SEC = max(60, int(os.getenv('DYNAMIC_EXIT_MIN_RATCHET_GAP_SEC', '120')))\n"
ratchet_block = ratchet_const + (
    "MIN_POSITION_AGE_BEFORE_RATCHET_SEC = max(120, int(os.getenv('DYNAMIC_EXIT_MIN_POSITION_AGE_SEC', '240')))\n"
    "EARLY_RATCHET_UNLOCK_PROFIT_PCT = max(0.0030, float(os.getenv('DYNAMIC_EXIT_EARLY_UNLOCK_PROFIT_PCT', '0.0050')))\n"
    "PROFIT_LOCK_ARM_R = max(0.55, float(os.getenv('DYNAMIC_EXIT_PROFIT_LOCK_ARM_R', '0.70')))\n"
    "PROFIT_LOCK_ARM_PCT = max(0.0040, float(os.getenv('DYNAMIC_EXIT_PROFIT_LOCK_ARM_PCT', '0.0060')))\n"
    "PROFIT_LOCK_KEEP = min(0.75, max(0.35, float(os.getenv('DYNAMIC_EXIT_PROFIT_LOCK_KEEP', '0.50'))))\n"
    "PROFIT_LOCK_STRONG_R = max(PROFIT_LOCK_ARM_R, float(os.getenv('DYNAMIC_EXIT_PROFIT_LOCK_STRONG_R', '1.35')))\n"
    "PROFIT_LOCK_STRONG_KEEP = min(0.85, max(PROFIT_LOCK_KEEP, float(os.getenv('DYNAMIC_EXIT_PROFIT_LOCK_STRONG_KEEP', '0.68'))))\n"
    "PROFIT_REVERSAL_GIVEBACK = min(0.60, max(0.20, float(os.getenv('DYNAMIC_EXIT_REVERSAL_GIVEBACK', '0.32'))))\n"
)
if 'PROFIT_LOCK_ARM_R =' not in d:
    if ratchet_const not in d:
        raise SystemExit('whipsaw patch failed: dynamic-exit constant marker missing')
    d = d.replace(ratchet_const, ratchet_block, 1)

# Extend 1m market context with the latest completed candle. high20 remains useful
# for structure calculations, but must never be treated as post-entry peak.
market_return_old = "    return {'bid': bid, 'ask': ask, 'high20': max(h[-20:]), 'swing_low7': min(l[-7:]), 'atr': atr}\n"
market_return_new = (
    "    last = rows[-1]\n"
    "    return {\n"
    "        'bid': bid, 'ask': ask, 'high20': max(h[-20:]), 'swing_low7': min(l[-7:]), 'atr': atr,\n"
    "        'atr_pct': atr / bid if bid > 0 else 0.0,\n"
    "        'last_open': float(last[1]), 'last_high': float(last[2]), 'last_low': float(last[3]), 'last_close': float(last[4]),\n"
    "        'last_open_time': int(last[0]) / 1000.0, 'last_close_time': int(last[6]) / 1000.0,\n"
    "        'last_red': float(last[4]) < float(last[1]),\n"
    "    }\n"
)
if "'last_red':" not in d:
    if market_return_old not in d:
        raise SystemExit('whipsaw patch failed: dynamic market return marker missing')
    d = d.replace(market_return_old, market_return_new, 1)

# Replace the stop suggestion function wholesale. This fixes the old bug where a
# pre-entry 20-minute high could be mistaken for profit earned after the BUY.
stop_start = d.find('def _suggest_stop(pos: dict, market: dict) -> tuple[float | None, str]:\n')
stop_end = d.find('\ndef _ids(signal_id: str, seq: int) -> tuple[str, str, str]:\n', stop_start)
if stop_start < 0 or stop_end < 0:
    raise SystemExit('whipsaw patch failed: _suggest_stop block missing')
new_suggest = r'''def _suggest_stop(pos: dict, market: dict) -> tuple[float | None, str]:
    entry = float(pos.get('entry') or 0.0)
    stop = float(pos.get('stop') or 0.0)
    target = float(pos.get('target') or 0.0)
    opened_at = float(pos.get('opened_at') or 0.0)
    previous_peak = max(float(pos.get('peak_price') or 0.0), entry)

    # Only count highs that happened after this position opened. The persisted
    # peak plus current bid makes this robust across polling cycles/restarts.
    post_entry_candle_high = 0.0
    if opened_at > 0 and float(market.get('last_open_time') or 0.0) >= opened_at:
        post_entry_candle_high = float(market.get('last_high') or 0.0)
    peak = max(previous_peak, float(market['bid']), post_entry_candle_high)

    if not (entry > stop > 0 and target > entry and peak > entry):
        return None, 'not-ready'

    r0 = float(pos.get('initial_risk_per_unit') or max(entry - stop, entry * 0.001))
    peak_gain = max(0.0, peak - entry)
    peak_gain_pct = peak_gain / entry
    r_mult = peak_gain / max(r0, 1e-12)
    progress = peak_gain / max(target - entry, 1e-12)
    bid = float(market['bid'])
    giveback = max(0.0, peak - bid) / max(peak_gain, 1e-12)

    suggested = stop
    reason = 'hold'

    # Existing milestone logic, but based strictly on post-entry peak.
    if r_mult >= 1.0 or progress >= 0.45:
        suggested = max(suggested, entry * 1.0015)
        reason = 'breakeven-plus'
    if r_mult >= 1.55 or progress >= 0.72:
        structure = float(market['swing_low7']) - 0.20 * float(market['atr'])
        suggested = max(suggested, structure, entry + 0.65 * r0)
        reason = 'structure-lock'
    if r_mult >= 2.0 or progress >= 0.88:
        structure = float(market['swing_low7']) - 0.12 * float(market['atr'])
        suggested = max(suggested, structure, entry + 1.10 * r0)
        reason = 'near-target-lock'

    # New giveback guard. Once meaningful profit exists, lock a fraction of the
    # best post-entry move. A red completed candle or a >=32% giveback upgrades
    # the lock immediately instead of waiting for the original TP/SL outcome.
    armed = r_mult >= PROFIT_LOCK_ARM_R or peak_gain_pct >= PROFIT_LOCK_ARM_PCT
    if armed:
        keep = PROFIT_LOCK_STRONG_KEEP if r_mult >= PROFIT_LOCK_STRONG_R else PROFIT_LOCK_KEEP
        reversal = bool(market.get('last_red')) and giveback >= PROFIT_REVERSAL_GIVEBACK
        if reversal:
            keep = max(keep, min(0.80, PROFIT_LOCK_STRONG_KEEP + 0.07))
            reason = 'red-reversal-profit-lock'
        elif giveback >= PROFIT_REVERSAL_GIVEBACK:
            keep = max(keep, PROFIT_LOCK_STRONG_KEEP)
            reason = 'profit-giveback-lock'
        elif reason == 'hold':
            reason = 'profit-lock'
        suggested = max(suggested, entry + peak_gain * keep)

    # Keep the stop safely below the current bid. The cushion adapts to 1m ATR:
    # tight enough to protect a reversal, not so tight that one normal tick exits.
    atr_pct = max(0.0, float(market.get('atr_pct') or 0.0))
    cushion = min(0.0040, max(0.0020, atr_pct * 0.75))
    suggested = min(suggested, bid * (1.0 - cushion))

    if suggested <= stop * 1.0015:
        return None, 'no-material-ratchet'
    return suggested, reason


'''
d = d[:stop_start] + new_suggest + d[stop_end + 1:]

# Track only post-entry peaks in run_once. high20 may contain candles before the
# position opened and was the root cause of premature/totally wrong ratchets.
peak_old = "            peak = max(float(pos.get('peak_price') or pos.get('entry') or 0.0), float(market['high20']), float(market['bid']))\n"
peak_new = (
    "            opened_at = float(pos.get('opened_at') or 0.0)\n"
    "            candle_high = float(market.get('last_high') or 0.0) if opened_at > 0 and float(market.get('last_open_time') or 0.0) >= opened_at else 0.0\n"
    "            peak = max(float(pos.get('peak_price') or pos.get('entry') or 0.0), float(market['bid']), candle_high)\n"
)
if 'candle_high = float(market.get(' not in d:
    if peak_old not in d:
        raise SystemExit('whipsaw patch failed: run_once peak marker missing')
    d = d.replace(peak_old, peak_new, 1)

# Fresh positions keep their original hard OCO during ordinary noise. However, a
# genuinely profitable spike is allowed to unlock the ratchet immediately so we
# do not give the whole move back during the first few minutes.
seq_marker = "    seq = int(pos.get('live_ratchet_seq') or 0) + 1\n"
age_guard = seq_marker + (
    "    opened_at = float(pos.get('opened_at') or 0.0)\n"
    "    position_age = time.time() - opened_at if opened_at > 0 else 0.0\n"
    "    entry = float(pos.get('entry') or 0.0)\n"
    "    lock_profit_pct = (suggested / entry - 1.0) if entry > 0 else 0.0\n"
    "    if opened_at > 0 and position_age < MIN_POSITION_AGE_BEFORE_RATCHET_SEC and lock_profit_pct < EARLY_RATCHET_UNLOCK_PROFIT_PCT:\n"
    "        trade_state.update_position(signal_id, shadow_suggested_stop=suggested, shadow_reason='early-ratchet-lock:' + reason, shadow_suggested_at=time.time())\n"
    "        print(f'[dynamic-exit] EARLY_LOCK {symbol} age={position_age:.0f}s<{MIN_POSITION_AGE_BEFORE_RATCHET_SEC}s lock={lock_profit_pct*100:.2f}% hard_oco_remains_active', flush=True)\n"
    "        return\n"
)
if '[dynamic-exit] EARLY_LOCK' not in d:
    if seq_marker not in d:
        raise SystemExit('whipsaw patch failed: dynamic-exit _replace marker missing')
    d = d.replace(seq_marker, age_guard, 1)

required_dynamic = [
    'PROFIT_LOCK_ARM_R =', 'PROFIT_LOCK_KEEP =', 'PROFIT_REVERSAL_GIVEBACK =',
    "'last_red':", 'post_entry_candle_high', 'red-reversal-profit-lock',
    'profit-giveback-lock', 'candle_high = float(market.get(',
    'EARLY_RATCHET_UNLOCK_PROFIT_PCT =', '[dynamic-exit] EARLY_LOCK',
]
for item in required_dynamic:
    if item not in d:
        raise SystemExit(f'whipsaw patch failed: missing dynamic marker {item}')
compile(d, str(dex_path), 'exec')
dex_path.write_text(d, encoding='utf-8')

print('[early-whipsaw-protection] OK 2-scan entry persistence + noise-aware hard stop + post-entry peak tracking + red-reversal/giveback profit lock')
