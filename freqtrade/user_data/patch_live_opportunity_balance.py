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

# The collector is explicitly SHADOW research telemetry and currently samples
# ~23/80 symbols. A 4h breadth of 0.30 on that incomplete snapshot was vetoing
# fully confirmed MID score-100 entries before the independent live flow/RR gates.
# Keep the conservative veto on reliable samples and on ALL sub-100 signals;
# only treat thin-sample breadth as advisory for an otherwise perfect candidate.
old_breadth = """        if lane == 'MID' and sideways_breadth4h < FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H:
            return _reject(
                'REGIME_REJECT', 'sideways-4h-breadth-weak',
                regime=regime, age=age, lane=lane, breadth_4h=sideways_breadth4h,
                required_breadth_4h=FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H,
            )
"""
new_breadth = """        if lane == 'MID' and sideways_breadth4h < FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H:
            try:
                breadth_sample_n = int(ctx.get('universe_n') or 0)
            except Exception:
                breadth_sample_n = 0
            if breadth_sample_n >= FALLBACK_MIN_RELIABLE_RANK_UNIVERSE or score < 100.0:
                return _reject(
                    'REGIME_REJECT', 'sideways-4h-breadth-weak',
                    regime=regime, age=age, lane=lane, breadth_4h=sideways_breadth4h,
                    required_breadth_4h=FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H,
                    context_universe_n=breadth_sample_n,
                )
            print(f'[sideways-breadth] ADVISORY thin_sample={breadth_sample_n} breadth4h={sideways_breadth4h:.2f} MID score={score:.0f}; live flow/RR/risk still required', flush=True)
"""
if new_breadth not in g:
    if old_breadth not in g:
        raise SystemExit('live-opportunity-balance: sideways breadth marker missing')
    g = g.replace(old_breadth, new_breadth, 1)

# Limit dollars exposed when bypassing the unreliable breadth reading.
flow_marker = "    flow = market_structure_flow.evaluate(symbol, payload, str(decision.get('regime') or ''))\n"
flow_with_cap = """    if lane == 'MID' and str(decision.get('regime') or '') == 'SIDEWAYS_COMPRESSION':
        try:
            context_now = market_context.symbol_context(symbol)
            sample_n = int(context_now.get('universe_n') or 0)
            breadth4 = float(context_now.get('breadth_4h') or 0.0)
            stake_before = float(payload.get('stakeUSDT') or 0.0)
            if sample_n < 40 and breadth4 < 0.55 and score >= 100.0 and stake_before > 7.0:
                payload['stakeUSDT'] = 7.0
                telemetry['thin_sideways_stake_cap'] = 7.0
                print(f'[sideways-breadth] STAKE_CAP {symbol} stake={stake_before:.2f}->7.00 thin_sample={sample_n}', flush=True)
        except Exception:
            pass
    flow = market_structure_flow.evaluate(symbol, payload, str(decision.get('regime') or ''))
"""
if flow_with_cap not in s:
    if flow_marker not in s:
        raise SystemExit('live-opportunity-balance: flow sizing marker missing')
    s = s.replace(flow_marker, flow_with_cap, 1)

for marker in ['source=adaptive-watch-promotion', 'FALLBACK_WEAK_BEAR_NORMAL_SCORE =', "lane not in {'NORMAL', 'MID', 'REVERSAL'}", 'weakbear_normal =', '[sideways-breadth] ADVISORY', 'thin_sideways_stake_cap']:
    if marker not in (s + g):
        raise SystemExit(f'live-opportunity-balance: missing marker {marker}')
compile(g, str(gate_path), 'exec')
compile(s, str(fast_path), 'exec')
gate_path.write_text(g, encoding='utf-8')
fast_path.write_text(s, encoding='utf-8')

# Build-time regression checks for the precise real-world failure mode. These
# are synthetic gate tests, NOT profitability tests and do not place orders.
import time as _time
import market_context as _mc
import spot_sniper_gate as _sg
_original_context = _mc.symbol_context
_original_ev = _sg.live_ev_gate.evaluate
try:
    _sg.live_ev_gate.evaluate = lambda _payload: {'enforced': False, 'passed': True, 'status': 'OBSERVE_ONLY'}
    _candidate = {'symbol': 'DASHUSDT', 'score': 100, 'strategy': 'MID_MOMENTUM_CONTINUATION', 'entry': 1.0, 'target': 1.02, 'stop': 0.99}
    def _sample(n, breadth, score):
        _mc.symbol_context = lambda _symbol: {'context_generated_at': _time.time(), 'regime': 'SIDEWAYS_COMPRESSION', 'universe_n': n, 'breadth_4h': breadth, 'breadth_1h': 0.50, 'opportunity_rank': 1}
        return _sg.evaluate(dict(_candidate, score=score))
    _thin_100 = _sample(23, 0.30, 100)
    _thin_99 = _sample(23, 0.30, 99)
    _reliable_100 = _sample(80, 0.30, 100)
    assert _thin_100['passed'] is True, _thin_100
    assert _thin_99['passed'] is False and _thin_99['reason'] == 'sideways-4h-breadth-weak', _thin_99
    assert _reliable_100['passed'] is False and _reliable_100['reason'] == 'sideways-4h-breadth-weak', _reliable_100
finally:
    _mc.symbol_context = _original_context
    _sg.live_ev_gate.evaluate = _original_ev

print('[live-opportunity-balance] PASS thin-sideways perfect MID advisory + reliable/low-score veto + capped stake; existing live RR/flow/OCO/risk unchanged')
