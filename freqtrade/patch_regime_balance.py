from pathlib import Path

p = Path('/freqtrade/spot_sniper_gate.py')
g = p.read_text(encoding='utf-8')

# The live market-context collector is intentionally heuristic/shadow telemetry.
# It may produce a small point-in-time sample when public kline requests are only
# partially successful. Therefore WEAK_BEAR must not be an unconditional veto on
# an otherwise fully-confirmed symbol-specific setup. PANIC remains a hard block.
#
# Policy:
# - WEAK_BEAR exception is limited to MID continuation and EARLY REVERSAL only.
# - MID needs score >=97; REVERSAL needs score >=98 after lane penalty.
# - NORMAL / EXPLOSIVE / EXTREME stay blocked in WEAK_BEAR.
# - With a reliable context sample (>=40 symbols), rank is still enforced and is
#   tighter in WEAK_BEAR (top 10).
# - With a thin context sample, rank is advisory; an unranked symbol needs 100.
# - All upstream BTC/HTF/microstructure/persistence and downstream portfolio/OCO
#   risk gates remain unchanged.

const_marker = "FALLBACK_HIGH_SCORE_MAX_RANK = max(FALLBACK_MAX_RANK, int(os.getenv('SPOT_SNIPER_HIGH_SCORE_MAX_RANK', '15')))\n"
const_extra = (
    "FALLBACK_WEAK_BEAR_BASE_SCORE = float(os.getenv('SPOT_SNIPER_WEAK_BEAR_BASE_SCORE', '95'))\n"
    "FALLBACK_WEAK_BEAR_MAX_RANK = max(1, int(os.getenv('SPOT_SNIPER_WEAK_BEAR_MAX_RANK', '10')))\n"
    "FALLBACK_MIN_RELIABLE_RANK_UNIVERSE = max(20, int(os.getenv('SPOT_SNIPER_MIN_RELIABLE_RANK_UNIVERSE', '40')))\n"
    "FALLBACK_THIN_CONTEXT_UNRANKED_SCORE = float(os.getenv('SPOT_SNIPER_THIN_CONTEXT_UNRANKED_SCORE', '100'))\n"
)
if 'FALLBACK_WEAK_BEAR_BASE_SCORE =' not in g:
    if const_marker not in g:
        raise SystemExit('regime-balance: constants marker missing')
    g = g.replace(const_marker, const_marker + const_extra, 1)

old_threshold = """def _fallback_threshold(regime: str, lane: str) -> float | None:
    if regime == 'STRONG_BULL':
        base = FALLBACK_STRONG_BULL_SCORE
    elif regime == 'WEAK_BULL':
        base = FALLBACK_WEAK_BULL_SCORE
    elif regime == 'POST_CRASH_RECOVERY':
        base = FALLBACK_POST_CRASH_SCORE
    elif regime == 'SIDEWAYS_COMPRESSION':
        base = FALLBACK_SIDEWAYS_SCORE
    else:
        return None

    penalty = {
"""
new_threshold = """def _fallback_threshold(regime: str, lane: str) -> float | None:
    if regime == 'STRONG_BULL':
        base = FALLBACK_STRONG_BULL_SCORE
    elif regime == 'WEAK_BULL':
        base = FALLBACK_WEAK_BULL_SCORE
    elif regime == 'POST_CRASH_RECOVERY':
        base = FALLBACK_POST_CRASH_SCORE
    elif regime == 'SIDEWAYS_COMPRESSION':
        base = FALLBACK_SIDEWAYS_SCORE
    elif regime == 'WEAK_BEAR':
        # Only symbol-specific relative-strength/reversal lanes may override the
        # broad weak tape, and only at exceptional lane scores.
        if lane not in {'MID', 'REVERSAL'}:
            return None
        base = FALLBACK_WEAK_BEAR_BASE_SCORE
    else:
        return None

    penalty = {
"""
if new_threshold not in g:
    if old_threshold not in g:
        raise SystemExit('regime-balance: fallback threshold marker missing')
    g = g.replace(old_threshold, new_threshold, 1)

old_rank = """    effective_max_rank = FALLBACK_HIGH_SCORE_MAX_RANK if score >= 98.0 else FALLBACK_MAX_RANK
    if rank_i is None or rank_i > effective_max_rank:
        return _reject(
            'FALLBACK_RANK_REJECT', f'production-fallback-rank>{effective_max_rank}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
        )

"""
new_rank = """    try:
        context_n = int(ctx.get('universe_n') or 0)
    except Exception:
        context_n = 0
    rank_reliable = context_n >= FALLBACK_MIN_RELIABLE_RANK_UNIVERSE
    effective_max_rank = FALLBACK_HIGH_SCORE_MAX_RANK if score >= 98.0 else FALLBACK_MAX_RANK
    if regime == 'WEAK_BEAR':
        effective_max_rank = min(effective_max_rank, FALLBACK_WEAK_BEAR_MAX_RANK)

    if rank_reliable and (rank_i is None or rank_i > effective_max_rank):
        return _reject(
            'FALLBACK_RANK_REJECT', f'production-fallback-rank>{effective_max_rank}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
            context_universe_n=context_n, rank_reliable=True,
        )

    # A thin snapshot must not become a global kill-switch. If the candidate was
    # not ranked at all, require a perfect lane score before allowing the normal
    # downstream safety gates to decide.
    if not rank_reliable and rank_i is None and score < FALLBACK_THIN_CONTEXT_UNRANKED_SCORE:
        return _reject(
            'FALLBACK_RANK_REJECT', 'thin-context-unranked-needs-score100',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
            context_universe_n=context_n, rank_reliable=False,
        )

"""
if new_rank not in g:
    if old_rank not in g:
        raise SystemExit('regime-balance: rank gate marker missing')
    g = g.replace(old_rank, new_rank, 1)

for marker in [
    'FALLBACK_WEAK_BEAR_BASE_SCORE =',
    "regime == 'WEAK_BEAR'",
    "lane not in {'MID', 'REVERSAL'}",
    'FALLBACK_MIN_RELIABLE_RANK_UNIVERSE',
    'thin-context-unranked-needs-score100',
]:
    if marker not in g:
        raise SystemExit(f'regime-balance: missing marker {marker}')

compile(g, str(p), 'exec')
p.write_text(g, encoding='utf-8')
print('[regime-balance] OK weak-bear is selective not absolute; MID>=97 REVERSAL>=98; panic unchanged; thin-context rank cannot deadlock score100')
