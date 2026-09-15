from pathlib import Path

engine_path = Path('/freqtrade/fast_entry_engine.py')
gate_path = Path('/freqtrade/spot_sniper_gate.py')

s = engine_path.read_text(encoding='utf-8')
g = gate_path.read_text(encoding='utf-8')

helper_marker = '\n\ndef _expert_pre_ingest(payload: dict) -> bool:\n'
helper = r'''

LANE_CONFIRM_HITS = max(2, int(os.getenv('FAST_LANE_CONFIRM_HITS', '2')))
LANE_CONFIRM_MIN_SEC = max(30, int(os.getenv('FAST_LANE_CONFIRM_MIN_SEC', '45')))
LANE_CONFIRM_MAX_GAP_SEC = max(LANE_CONFIRM_MIN_SEC + 15, int(os.getenv('FAST_LANE_CONFIRM_MAX_GAP_SEC', '150')))
_lane_confirm_state: dict[str, dict] = {}


def _lane_confirmation_ready(lane: str, symbol: str, score: float, price: float) -> bool:
    lane = str(lane or 'NORMAL').upper()
    if lane == 'NORMAL':
        return True
    now = time.time()
    key = f'{lane}:{symbol}'
    row = _lane_confirm_state.get(key)
    if row is None or now - float(row.get('last_at') or 0.0) > LANE_CONFIRM_MAX_GAP_SEC:
        _lane_confirm_state[key] = {
            'first_at': now, 'last_at': now, 'hits': 1,
            'first_score': float(score), 'last_score': float(score),
            'first_price': float(price or 0.0),
        }
        print(f'[lane-confirm] ARM {lane} {symbol} score={score:.0f} hits=1/{LANE_CONFIRM_HITS} min_age={LANE_CONFIRM_MIN_SEC}s', flush=True)
        return False
    if now - float(row.get('last_at') or 0.0) < 20:
        return False
    row['last_at'] = now
    row['last_score'] = float(score)
    row['hits'] = int(row.get('hits') or 1) + 1
    age = now - float(row.get('first_at') or now)
    first_price = float(row.get('first_price') or price or 0.0)
    price_ok = first_price <= 0 or float(price or 0.0) >= first_price * 0.995
    if int(row['hits']) >= LANE_CONFIRM_HITS and age >= LANE_CONFIRM_MIN_SEC and price_ok:
        _lane_confirm_state.pop(key, None)
        print(f'[lane-confirm] PASS {lane} {symbol} score={score:.0f} hits={row["hits"]} age={age:.0f}s -> final gates', flush=True)
        return True
    if not price_ok:
        _lane_confirm_state.pop(key, None)
        print(f'[lane-confirm] RESET {lane} {symbol} reason=price-faded-before-confirm', flush=True)
        return False
    print(f'[lane-confirm] HOLD {lane} {symbol} score={score:.0f} hits={row["hits"]}/{LANE_CONFIRM_HITS} age={age:.0f}s', flush=True)
    return False

'''
if 'def _lane_confirmation_ready(' not in s:
    if helper_marker not in s:
        raise SystemExit('final-entry-hardening: _expert_pre_ingest marker missing')
    s = s.replace(helper_marker, helper + helper_marker, 1)

sig_pos = s.find('def _expert_pre_ingest(payload: dict) -> bool:\n')
if sig_pos < 0:
    raise SystemExit('final-entry-hardening: final expert gate missing')
decision_marker = "    decision = spot_sniper_gate.evaluate(payload)\n"
decision_pos = s.find(decision_marker, sig_pos)
if decision_pos < 0:
    raise SystemExit('final-entry-hardening: Spot Sniper decision marker missing')
confirm_block = (
    "    if not _lane_confirmation_ready(lane, symbol, score, price):\n"
    "        _record_candidate(symbol, lane, score, price, 'HOLD', 'persistent-confirmation-pending')\n"
    "        return False\n"
)
if confirm_block not in s[sig_pos:decision_pos + len(decision_marker)]:
    s = s[:decision_pos] + confirm_block + s[decision_pos:]

old_mid = "    mult = clamp(MID_STAKE_MULT, 0.40, 0.90)\n    stake = math.floor(base_stake * mult * 100.0) / 100.0\n"
new_mid = "    mult = 1.0\n    stake = math.floor(base_stake * 100.0) / 100.0\n"
if old_mid in s:
    s = s.replace(old_mid, new_mid, 1)
elif new_mid not in s:
    raise SystemExit('final-entry-hardening: MID sizing marker missing')

const_marker = "FALLBACK_EXTREME_PENALTY = float(os.getenv('SPOT_SNIPER_FALLBACK_EXTREME_PENALTY', '5'))\n"
const_extra = (
    "FALLBACK_REVERSAL_PENALTY = float(os.getenv('SPOT_SNIPER_FALLBACK_REVERSAL_PENALTY', '3'))\n"
    "FALLBACK_MAX_RANK = max(1, int(os.getenv('SPOT_SNIPER_FALLBACK_MAX_RANK', '10')))\n"
    "FALLBACK_SIDEWAYS_SCORE = float(os.getenv('SPOT_SNIPER_FALLBACK_SIDEWAYS_SCORE', '96'))\n"
    "FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H = max(0.0, min(1.0, float(os.getenv('SPOT_SNIPER_SIDEWAYS_MID_MIN_BREADTH_4H', '0.55'))))\n"
    "FALLBACK_HIGH_SCORE_MAX_RANK = max(FALLBACK_MAX_RANK, int(os.getenv('SPOT_SNIPER_HIGH_SCORE_MAX_RANK', '15')))\n"
)
if 'FALLBACK_MAX_RANK =' not in g:
    if const_marker not in g:
        raise SystemExit('final-entry-hardening: Spot Sniper constants marker missing')
    g = g.replace(const_marker, const_marker + const_extra, 1)

lane_marker = "    if strategy.startswith('EXTREME_CONTINUATION'):\n        return 'EXTREME'\n"
if "strategy.startswith('EARLY_REVERSAL_STARTER')" not in g:
    if lane_marker not in g:
        raise SystemExit('final-entry-hardening: Spot Sniper lane marker missing')
    g = g.replace(lane_marker, lane_marker + "    if strategy.startswith('EARLY_REVERSAL_STARTER'):\n        return 'REVERSAL'\n", 1)

penalty_old = "        'EXTREME': FALLBACK_EXTREME_PENALTY,\n    }.get(lane, FALLBACK_EXTREME_PENALTY)\n"
penalty_new = "        'EXTREME': FALLBACK_EXTREME_PENALTY,\n        'REVERSAL': FALLBACK_REVERSAL_PENALTY,\n    }.get(lane, FALLBACK_EXTREME_PENALTY)\n"
if penalty_old in g:
    g = g.replace(penalty_old, penalty_new, 1)
elif penalty_new not in g:
    raise SystemExit('final-entry-hardening: lane penalty marker missing')

regime_old = "    elif regime == 'POST_CRASH_RECOVERY':\n        base = FALLBACK_POST_CRASH_SCORE\n    else:\n"
regime_new = "    elif regime == 'POST_CRASH_RECOVERY':\n        base = FALLBACK_POST_CRASH_SCORE\n    elif regime == 'SIDEWAYS_COMPRESSION':\n        base = FALLBACK_SIDEWAYS_SCORE\n    else:\n"
if regime_new not in g:
    if regime_old not in g:
        raise SystemExit('final-entry-hardening: fallback regime marker missing')
    g = g.replace(regime_old, regime_new, 1)

sideways_old = "    if SIDEWAYS_BLOCK and regime == 'SIDEWAYS_COMPRESSION':\n        return _reject(\n            'REGIME_REJECT', 'sideways-compression-no-validated-edge',\n            regime=regime, age=age, lane=lane,\n        )\n"
sideways_new = "    if SIDEWAYS_BLOCK and regime == 'SIDEWAYS_COMPRESSION':\n        sideways_required = _fallback_threshold(regime, lane)\n        try:\n            sideways_breadth4h = float(ctx.get('breadth_4h') or 0.0)\n        except Exception:\n            sideways_breadth4h = 0.0\n        if lane == 'MID' and sideways_breadth4h < FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H:\n            return _reject(\n                'REGIME_REJECT', 'sideways-4h-breadth-weak',\n                regime=regime, age=age, lane=lane, breadth_4h=sideways_breadth4h,\n                required_breadth_4h=FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H,\n            )\n        if sideways_required is None or score < sideways_required:\n            return _reject(\n                'REGIME_REJECT', 'sideways-compression-quality-below-exception',\n                regime=regime, age=age, lane=lane, fallback_score_required=sideways_required,\n            )\n"
if 'sideways-4h-breadth-weak' not in g:
    if sideways_old not in g:
        raise SystemExit('final-entry-hardening: sideways block marker missing')
    g = g.replace(sideways_old, sideways_new, 1)

score_gate = "    if score < required_score:\n"
rank_gate = (
    "    effective_max_rank = FALLBACK_HIGH_SCORE_MAX_RANK if score >= 98.0 else FALLBACK_MAX_RANK\n"
    "    if rank_i is None or rank_i > effective_max_rank:\n"
    "        return _reject(\n"
    "            'FALLBACK_RANK_REJECT', f'production-fallback-rank>{effective_max_rank}',\n"
    "            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,\n"
    "            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,\n"
    "        )\n\n"
)
legacy_rank_gate = (
    "    if rank_i is None or rank_i > FALLBACK_MAX_RANK:\n"
    "        return _reject(\n"
    "            'FALLBACK_RANK_REJECT', f'production-fallback-rank>{FALLBACK_MAX_RANK}',\n"
    "            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,\n"
    "            fallback_score_required=required_score, fallback_max_rank=FALLBACK_MAX_RANK,\n"
    "        )\n\n"
)
if rank_gate not in g:
    if legacy_rank_gate in g:
        g = g.replace(legacy_rank_gate, rank_gate, 1)
    else:
        pos = g.find(score_gate)
        if pos < 0:
            raise SystemExit('final-entry-hardening: fallback score marker missing')
        g = g[:pos] + rank_gate + g[pos:]

for marker in ['def _lane_confirmation_ready(', "'persistent-confirmation-pending'", 'mult = 1.0']:
    if marker not in s:
        raise SystemExit(f'final-entry-hardening: missing engine marker {marker}')
for marker in [
    'FALLBACK_MAX_RANK =', 'FALLBACK_SIDEWAYS_SCORE =', 'FALLBACK_SIDEWAYS_MID_MIN_BREADTH_4H =',
    'FALLBACK_HIGH_SCORE_MAX_RANK =', "'FALLBACK_RANK_REJECT'",
    "strategy.startswith('EARLY_REVERSAL_STARTER')", "regime == 'SIDEWAYS_COMPRESSION'",
    "'sideways-4h-breadth-weak'", 'effective_max_rank =',
]:
    if marker not in g:
        raise SystemExit(f'final-entry-hardening: missing gate marker {marker}')

compile(s, str(engine_path), 'exec')
compile(g, str(gate_path), 'exec')
engine_path.write_text(s, encoding='utf-8')
gate_path.write_text(g, encoding='utf-8')
print('[final-entry-hardening] OK persistent confirm + SIDEWAYS MID breadth4h>=0.55 + score98 rank<=15 + CRCLB far-rank protection')
