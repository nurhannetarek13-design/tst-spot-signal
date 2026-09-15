from pathlib import Path

fast_path = Path('/freqtrade/fast_entry_engine.py')
s = fast_path.read_text(encoding='utf-8')

start = s.find('def _direct_confirmation_ready(symbol: str, score: float, m: dict) -> bool:\n')
end = s.find('\ndef _watch_live_price(m: dict) -> float:\n', start)
if start < 0 or end < 0:
    raise SystemExit('live-opportunity-balance: direct confirmation block missing')

new_direct = r'''def _direct_confirmation_ready(symbol: str, score: float, m: dict) -> bool:
    now = time.time()
    price = _watch_live_price(m)
    prior = None
    for rec in reversed(near_miss_records):
        if str(rec.get('symbol') or '') == symbol:
            prior = rec
            break
    if prior is not None and float(score) >= DIRECT_SCORE:
        started = float(prior.get('start_at') or 0.0)
        first_price = float(prior.get('start_price') or 0.0)
        first_score = float(prior.get('start_score') or 0.0)
        age = now - started if started > 0 else 0.0
        price_ok = first_price <= 0 or price >= first_price * 0.995
        age_ok = DIRECT_CONFIRM_MIN_SEC <= age <= (WATCH_TTL_SEC + DIRECT_CONFIRM_MAX_GAP_SEC)
        if first_score >= WATCH_SCORE and age_ok and price_ok:
            direct_confirm_state.pop(symbol, None)
            print(f'[direct-confirm] PASS {symbol} score={score:.0f} source=adaptive-watch-promotion age={age:.0f}s -> final gates')
            return True

    row = direct_confirm_state.get(symbol)
    if row is None or now - float(row.get('last_at') or 0.0) > DIRECT_CONFIRM_MAX_GAP_SEC:
        direct_confirm_state[symbol] = {
            'first_at': now, 'last_at': now, 'hits': 1,
            'first_score': float(score), 'last_score': float(score),
            'first_price': price,
        }
        print(f'[direct-confirm] ARM {symbol} score={score:.0f} hits=1/{DIRECT_CONFIRM_HITS} min_age={DIRECT_CONFIRM_MIN_SEC}s')
        return False
    if now - float(row.get('last_at') or 0.0) < 20:
        return False
    row['last_at'] = now
    row['last_score'] = float(score)
    row['hits'] = int(row.get('hits') or 1) + 1
    age = now - float(row.get('first_at') or now)
    first_price = float(row.get('first_price') or price or 0.0)
    price_ok = first_price <= 0 or price >= first_price * 0.995
    if int(row['hits']) >= DIRECT_CONFIRM_HITS and age >= DIRECT_CONFIRM_MIN_SEC and price_ok:
        direct_confirm_state.pop(symbol, None)
        print(f'[direct-confirm] PASS {symbol} score={score:.0f} hits={row["hits"]} age={age:.0f}s -> final gates')
        return True
    if not price_ok:
        direct_confirm_state.pop(symbol, None)
        print(f'[direct-confirm] RESET {symbol} reason=price-faded-before-confirm')
        return False
    print(f'[direct-confirm] HOLD {symbol} score={score:.0f} hits={row["hits"]}/{DIRECT_CONFIRM_HITS} age={age:.0f}s')
    return False

'''
s = s[:start] + new_direct + s[end + 1:]
compile(s, str(fast_path), 'exec')
fast_path.write_text(s, encoding='utf-8')

gate_path = Path('/freqtrade/spot_sniper_gate.py')
g = gate_path.read_text(encoding='utf-8')
const_marker = "FALLBACK_WEAK_BEAR_BASE_SCORE = float(os.getenv('SPOT_SNIPER_WEAK_BEAR_BASE_SCORE', '95'))\n"
if 'FALLBACK_WEAK_BEAR_NORMAL_SCORE =' not in g:
    if const_marker not in g:
        raise SystemExit('live-opportunity-balance: weak-bear constant marker missing')
    g = g.replace(const_marker, const_marker + "FALLBACK_WEAK_BEAR_NORMAL_SCORE = float(os.getenv('SPOT_SNIPER_WEAK_BEAR_NORMAL_SCORE', '96'))\n", 1)

old = """    elif regime == 'WEAK_BEAR':
        # Only symbol-specific relative-strength/reversal lanes may override the
        # broad weak tape, and only at exceptional lane scores.
        if lane not in {'MID', 'REVERSAL'}:
            return None
        base = FALLBACK_WEAK_BEAR_BASE_SCORE
"""
new = """    elif regime == 'WEAK_BEAR':
        if lane not in {'NORMAL', 'MID', 'REVERSAL'}:
            return None
        base = FALLBACK_WEAK_BEAR_NORMAL_SCORE if lane == 'NORMAL' else FALLBACK_WEAK_BEAR_BASE_SCORE
"""
if new not in g:
    if old not in g:
        raise SystemExit('live-opportunity-balance: weak-bear threshold marker missing')
    g = g.replace(old, new, 1)

old_thin = """    recovery_reversal = regime in {'PANIC_HIGH_VOL_BEAR', 'SIDEWAYS_COMPRESSION'} and lane == 'REVERSAL'
    thin_unranked_required = required_score if recovery_reversal else FALLBACK_THIN_CONTEXT_UNRANKED_SCORE
"""
new_thin = """    recovery_reversal = regime in {'PANIC_HIGH_VOL_BEAR', 'SIDEWAYS_COMPRESSION'} and lane == 'REVERSAL'
    weakbear_normal = regime == 'WEAK_BEAR' and lane == 'NORMAL'
    thin_unranked_required = (
        required_score if recovery_reversal
        else max(required_score, 98.0) if weakbear_normal
        else FALLBACK_THIN_CONTEXT_UNRANKED_SCORE
    )
"""
if new_thin not in g:
    if old_thin not in g:
        raise SystemExit('live-opportunity-balance: thin context marker missing')
    g = g.replace(old_thin, new_thin, 1)

for marker in ['source=adaptive-watch-promotion', 'FALLBACK_WEAK_BEAR_NORMAL_SCORE =', "lane not in {'NORMAL', 'MID', 'REVERSAL'}", 'weakbear_normal =']:
    if marker not in (s + g):
        raise SystemExit(f'live-opportunity-balance: missing marker {marker}')
compile(g, str(gate_path), 'exec')
gate_path.write_text(g, encoding='utf-8')
print('[live-opportunity-balance] OK adaptive promotion counts as persistence + WEAK_BEAR NORMAL score>=96/unranked>=98; panic and hard risk unchanged')
