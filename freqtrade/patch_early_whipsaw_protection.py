from pathlib import Path

# Reduce 1-2 minute losing whipsaws without ever removing the hard exchange stop.
# Protection has three layers:
#   1) require a second direct-grade scan before a normal-lane BUY can be emitted;
#   2) make the initial normal-lane stop wide enough to clear ordinary 1m noise,
#      while dynamic sizing automatically reduces stake to keep dollar risk capped;
#   3) do not ratchet/tighten an OCO during the first few minutes after fill.

fast_path = Path('/freqtrade/fast_entry_engine.py')
s = fast_path.read_text(encoding='utf-8')

# ---------------------------------------------------------------------------
# Runtime knobs. Defaults are intentionally conservative and can only be changed
# explicitly through Railway variables.
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
    """Return a noise-aware protective stop distance as a decimal fraction.

    The stop is never removed. We only prevent the normal lane from using a stop
    that sits inside ordinary 1-minute noise. Because recommended_stake() sizes
    from stop distance, a wider stop automatically means a smaller position and
    therefore does not increase the configured USDT risk cap.
    """
    try:
        atr = float(m.get('atr_pct') or m.get('atr') or 0.0)
    except Exception:
        atr = 0.0
    floor = max(WHIPSAW_MIN_STOP_PCT, atr * WHIPSAW_ATR_MULT)
    return clamp(max(float(proposed), floor), WHIPSAW_MIN_STOP_PCT, WHIPSAW_MAX_STOP_PCT)


def _direct_confirmation_ready(symbol: str, score: float, m: dict) -> bool:
    """Require persistent direct-grade pressure across two scanner cycles.

    A one-scan spike is logged but cannot immediately become a BUY. A candidate
    must remain direct-grade for at least DIRECT_CONFIRM_MIN_SEC; stale/gapped
    confirmation state is discarded. maybe_signal() still re-checks every hard
    quality, BTC, regime, microstructure, sizing and execution gate afterwards.
    """
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

    # Avoid counting the same scanner moment twice if multiple code paths touch it.
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

# Require the current market-metrics object in the normal direct loop so it can
# be used by the persistence gate without an extra API call.
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

# The normal lane previously allowed ~0.55%-0.90% stops. That can sit inside one
# or two ordinary 1m candles. Widen only as much as live 1m ATR/noise requires,
# then preserve a minimum reward:risk ratio. Dynamic sizing reduces stake if the
# stop widens, so configured max USDT risk remains unchanged.
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
# Dynamic exit: never tighten/replace the OCO in the first five minutes. The
# original hard OCO remains active the whole time, so this is not a no-stop hold.
# ---------------------------------------------------------------------------
dex_path = Path('/freqtrade/dynamic_exit_manager.py')
d = dex_path.read_text(encoding='utf-8')

ratchet_const = "MIN_RATCHET_GAP_SEC = max(60, int(os.getenv('DYNAMIC_EXIT_MIN_RATCHET_GAP_SEC', '120')))\n"
ratchet_block = ratchet_const + "MIN_POSITION_AGE_BEFORE_RATCHET_SEC = max(180, int(os.getenv('DYNAMIC_EXIT_MIN_POSITION_AGE_SEC', '300')))\n"
if 'MIN_POSITION_AGE_BEFORE_RATCHET_SEC =' not in d:
    if ratchet_const not in d:
        raise SystemExit('whipsaw patch failed: dynamic-exit constant marker missing')
    d = d.replace(ratchet_const, ratchet_block, 1)

seq_marker = "    seq = int(pos.get('live_ratchet_seq') or 0) + 1\n"
age_guard = seq_marker + (
    "    opened_at = float(pos.get('opened_at') or 0.0)\n"
    "    position_age = time.time() - opened_at if opened_at > 0 else 0.0\n"
    "    if opened_at > 0 and position_age < MIN_POSITION_AGE_BEFORE_RATCHET_SEC:\n"
    "        trade_state.update_position(signal_id, shadow_suggested_stop=suggested, shadow_reason='early-ratchet-lock:' + reason, shadow_suggested_at=time.time())\n"
    "        print(f'[dynamic-exit] EARLY_LOCK {symbol} age={position_age:.0f}s<{MIN_POSITION_AGE_BEFORE_RATCHET_SEC}s hard_oco_remains_active', flush=True)\n"
    "        return\n"
)
if '[dynamic-exit] EARLY_LOCK' not in d:
    if seq_marker not in d:
        raise SystemExit('whipsaw patch failed: dynamic-exit _replace marker missing')
    d = d.replace(seq_marker, age_guard, 1)

for item in ['MIN_POSITION_AGE_BEFORE_RATCHET_SEC =', '[dynamic-exit] EARLY_LOCK', 'hard_oco_remains_active']:
    if item not in d:
        raise SystemExit(f'whipsaw patch failed: missing dynamic-exit marker {item}')
compile(d, str(dex_path), 'exec')
dex_path.write_text(d, encoding='utf-8')

print('[early-whipsaw-protection] OK 2-scan entry persistence + ATR/noise stop floor + 5m ratchet lock; hard OCO always active')
