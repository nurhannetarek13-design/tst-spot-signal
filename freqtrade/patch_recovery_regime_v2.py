from pathlib import Path

p = Path('/freqtrade/spot_sniper_gate.py')
g = p.read_text(encoding='utf-8')

# Recovery-regime hardening:
# market_context.py explicitly labels its regime as heuristic/shadow-only.  It is
# useful context, but it must not become a stale 1h/4h kill-switch while the live
# tape has already turned.  Confirm PANIC with the independent live BTC hard-risk
# preflight, and allow only the dedicated EARLY_REVERSAL lane when that hard risk
# has cleared.  NORMAL/MID/EXPLOSIVE/EXTREME remain blocked in PANIC.
#
# SIDEWAYS immediately after a selloff is also a normal recovery state.  The
# reversal lane already has its own selloff/reclaim/volume/taker/HTF/BTC checks,
# persistent confirmation, live order-flow/profile/Wyckoff gate and reduced
# starter sizing.  Do not require an additional score=99 merely because the
# lagging 1h/4h regime is SIDEWAYS.

if 'import entry_quality\n' not in g:
    marker = 'import live_ev_gate\nimport market_context\n'
    if marker not in g:
        raise SystemExit('recovery-regime-v2: import marker missing')
    g = g.replace(marker, 'import entry_quality\n' + marker, 1)

const_marker = "FALLBACK_THIN_CONTEXT_UNRANKED_SCORE = float(os.getenv('SPOT_SNIPER_THIN_CONTEXT_UNRANKED_SCORE', '100'))\n"
const_extra = (
    "FALLBACK_RECOVERY_REVERSAL_BASE_SCORE = float(os.getenv('SPOT_SNIPER_RECOVERY_REVERSAL_BASE_SCORE', '92'))\n"
)
if 'FALLBACK_RECOVERY_REVERSAL_BASE_SCORE =' not in g:
    if const_marker not in g:
        raise SystemExit('recovery-regime-v2: constant marker missing')
    g = g.replace(const_marker, const_marker + const_extra, 1)

old_threshold = """    elif regime == 'SIDEWAYS_COMPRESSION':
        base = FALLBACK_SIDEWAYS_SCORE
    elif regime == 'WEAK_BEAR':
        # Only symbol-specific relative-strength/reversal lanes may override the
        # broad weak tape, and only at exceptional lane scores.
        if lane not in {'MID', 'REVERSAL'}:
            return None
        base = FALLBACK_WEAK_BEAR_BASE_SCORE
    else:
        return None
"""
new_threshold = """    elif regime == 'SIDEWAYS_COMPRESSION':
        # A confirmed reversal after a selloff must not need score=99 simply
        # because the lagging 1h/4h context has not caught up with the turn.
        base = FALLBACK_RECOVERY_REVERSAL_BASE_SCORE if lane == 'REVERSAL' else FALLBACK_SIDEWAYS_SCORE
    elif regime == 'WEAK_BEAR':
        # Only symbol-specific relative-strength/reversal lanes may override the
        # broad weak tape, and only at exceptional lane scores.
        if lane not in {'MID', 'REVERSAL'}:
            return None
        base = FALLBACK_WEAK_BEAR_BASE_SCORE
    elif regime == 'PANIC_HIGH_VOL_BEAR':
        # PANIC from market_context is heuristic/shadow.  Only the dedicated
        # reversal lane may continue, and evaluate() separately requires the
        # current live BTC hard-risk preflight to have cleared first.
        if lane != 'REVERSAL':
            return None
        base = FALLBACK_RECOVERY_REVERSAL_BASE_SCORE
    else:
        return None
"""
if new_threshold not in g:
    if old_threshold not in g:
        raise SystemExit('recovery-regime-v2: threshold marker missing')
    g = g.replace(old_threshold, new_threshold, 1)

old_panic = """    if PANIC_BLOCK and regime == 'PANIC_HIGH_VOL_BEAR':
        return _reject('REGIME_REJECT', 'panic-high-vol-bear', regime=regime, age=age, lane=lane)
"""
new_panic = """    if PANIC_BLOCK and regime == 'PANIC_HIGH_VOL_BEAR':
        # The 1h/4h heuristic can stay PANIC for several minutes after a sharp
        # market rebound has begun.  Confirm actual danger with the independent
        # live BTC preflight instead of letting shadow telemetry veto forever.
        try:
            panic_pf = entry_quality.runtime_preflight()
            live_btc_hard = not bool(panic_pf.get('btc_regime_ok'))
        except Exception as exc:
            return _reject(
                'REGIME_REJECT', f'panic-live-btc-preflight-failed:{type(exc).__name__}',
                regime=regime, age=age, lane=lane,
            )
        if live_btc_hard:
            return _reject(
                'REGIME_REJECT', 'panic-live-btc-hard',
                regime=regime, age=age, lane=lane,
                btc_tier=panic_pf.get('btc_tier'),
            )
        if lane != 'REVERSAL':
            return _reject(
                'REGIME_REJECT', 'panic-shadow-recovery-reversal-only',
                regime=regime, age=age, lane=lane,
                btc_tier=panic_pf.get('btc_tier'),
            )
"""
if new_panic not in g:
    if old_panic not in g:
        raise SystemExit('recovery-regime-v2: panic marker missing')
    g = g.replace(old_panic, new_panic, 1)

old_thin = """    if not rank_reliable and rank_i is None and score < FALLBACK_THIN_CONTEXT_UNRANKED_SCORE:
        return _reject(
            'FALLBACK_RANK_REJECT', 'thin-context-unranked-needs-score100',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
            context_universe_n=context_n, rank_reliable=False,
        )
"""
new_thin = """    recovery_reversal = regime in {'PANIC_HIGH_VOL_BEAR', 'SIDEWAYS_COMPRESSION'} and lane == 'REVERSAL'
    thin_unranked_required = required_score if recovery_reversal else FALLBACK_THIN_CONTEXT_UNRANKED_SCORE
    if not rank_reliable and rank_i is None and score < thin_unranked_required:
        return _reject(
            'FALLBACK_RANK_REJECT', f'thin-context-unranked-needs-score{thin_unranked_required:.0f}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
            context_universe_n=context_n, rank_reliable=False,
        )
"""
if new_thin not in g:
    if old_thin not in g:
        raise SystemExit('recovery-regime-v2: thin-context marker missing')
    g = g.replace(old_thin, new_thin, 1)

for marker in [
    'import entry_quality',
    'FALLBACK_RECOVERY_REVERSAL_BASE_SCORE =',
    "regime == 'PANIC_HIGH_VOL_BEAR'",
    "'panic-live-btc-hard'",
    "'panic-shadow-recovery-reversal-only'",
    'recovery_reversal =',
]:
    if marker not in g:
        raise SystemExit(f'recovery-regime-v2: missing marker {marker}')

compile(g, str(p), 'exec')
p.write_text(g, encoding='utf-8')
print('[recovery-regime-v2] OK shadow PANIC requires live BTC hard confirmation; recovered PANIC/SIDEWAYS allow confirmed REVERSAL score>=95 only')
