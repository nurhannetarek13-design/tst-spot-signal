from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'MANUAL_FALLBACK_MIN_SCORE' in s:
    print('[manual-confirm-fallback] already applied')
    raise SystemExit(0)

# Manual-only bridge while calibrated EV evidence is warming up. This never
# enables auto-buy and does not weaken quality, API health, daily-loss,
# loss-streak, drawdown, position-count, stop-risk, OCO or confirmation gates.
const_anchor = "PORTFOLIO_MAX_POSITIONS = int(os.getenv('FAST_PORTFOLIO_MAX_POSITIONS', '3'))\n"
const_block = const_anchor + (
    "MANUAL_FALLBACK_MIN_SCORE = float(os.getenv('FAST_MANUAL_FALLBACK_MIN_SCORE', '80'))\n"
    "MANUAL_FALLBACK_TOP_N = max(1, min(20, int(os.getenv('FAST_MANUAL_FALLBACK_TOP_N', '10'))))\n"
    "MANUAL_FALLBACK_ENABLED = os.getenv('FAST_MANUAL_FALLBACK_ENABLED', '1').strip() == '1'\n"
)
if const_anchor not in s:
    raise SystemExit('manual-confirm fallback failed: constants anchor missing')
s = s.replace(const_anchor, const_block, 1)

# patch_signal_visibility runs immediately before us, so target only its stable
# two-line tail rather than the whole Spot Sniper wrapper.
reject_tail = (
    "        _signal_visibility_alert(symbol, score, 'DIRECT_BLOCKED', price=price, reason=why, regime=str(decision.get('regime') or ''))\n"
    "        return False\n"
)
replacement = r'''        _signal_visibility_alert(symbol, score, 'DIRECT_BLOCKED', price=price, reason=why, regime=str(decision.get('regime') or ''))

        reject_reason = str(decision.get('reason') or decision.get('status') or 'reject')
        ctx = market_context.symbol_context(symbol) or {}
        try:
            ctx_generated = float(ctx.get('context_generated_at') or 0.0)
            ctx_age = time.time() - ctx_generated if ctx_generated > 0 else 1e12
            ctx_rank = int(ctx.get('opportunity_rank')) if ctx.get('opportunity_rank') is not None else None
        except Exception:
            ctx_age = 1e12
            ctx_rank = None
        regime = str(ctx.get('regime') or decision.get('regime') or 'UNKNOWN')

        # Only rescue lack-of-model-evidence / Sideways authorization. Never
        # rescue panic, enforced-EV rejection, stale context, poor rank, or a
        # genuine hard-risk rejection.
        warmup_or_sideways = (
            str(decision.get('status') or '') == 'WARMUP_BLOCK'
            or reject_reason == 'sideways-compression-no-validated-edge'
            or reject_reason.startswith('ev-not-live-enforced:')
        )
        manual_ok = (
            MANUAL_FALLBACK_ENABLED
            and score >= MANUAL_FALLBACK_MIN_SCORE
            and warmup_or_sideways
            and regime != 'PANIC_HIGH_VOL_BEAR'
            and ctx_age <= spot_sniper_gate.MAX_CONTEXT_AGE_SEC
            and ctx_rank is not None
            and ctx_rank <= MANUAL_FALLBACK_TOP_N
            and str(ev.get('status') or '') != 'ENFORCED_REJECT'
        )
        if not manual_ok:
            return False

        payload['manualFallback'] = True
        payload['manualFallbackReason'] = reject_reason
        payload['manualFallbackRank'] = ctx_rank
        payload['manualFallbackRegime'] = regime
        payload['strategy'] = str(payload.get('strategy') or '') + '|MANUAL_CONFIRM_FALLBACK'
        print(
            f'[manual-fallback] {symbol} ELIGIBLE score={score:.0f} rank={ctx_rank} '
            f'regime={regime} reason={reject_reason} autoBuy=False confirmation=REQUIRED',
            flush=True,
        )
'''
if reject_tail not in s:
    raise SystemExit('manual-confirm fallback failed: signal-visibility reject tail missing')
s = s.replace(reject_tail, replacement, 1)

ready_old = "    _record_candidate(symbol, lane, score, price, 'READY', 'spot-sniper-pass|' + str(why), **telemetry)\n"
ready_new = "    _record_candidate(symbol, lane, score, price, 'READY', ('manual-confirm-fallback|' if payload.get('manualFallback') else 'spot-sniper-pass|') + str(why), **telemetry)\n"
if ready_old in s:
    s = s.replace(ready_old, ready_new, 1)

for required in [
    'MANUAL_FALLBACK_MIN_SCORE',
    'MANUAL_FALLBACK_TOP_N',
    "payload['manualFallback'] = True",
    '[manual-fallback]',
    "str(ev.get('status') or '') != 'ENFORCED_REJECT'",
    '_portfolio_allows(payload)',
]:
    if required not in s:
        raise SystemExit(f'manual-confirm fallback failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[manual-confirm-fallback] OK score>=80 top10 warmup/sideways manual-only path; all hard risk/execution gates unchanged')
