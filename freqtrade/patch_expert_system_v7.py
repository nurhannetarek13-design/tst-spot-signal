from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

marker = '\ndef validate_and_resolve_telegram_chat() -> bool:\n'
override = r'''

# v7 research-integrity override: public derivatives context and the counterfactual
# lane-health sampler are useful telemetry, but neither has passed frozen OOS /
# walk-forward validation. They must not alter live entry eligibility.
_derivatives_adjust_shadow_source = _derivatives_adjust


def _derivatives_adjust(symbol: str, score: float) -> tuple[float, str]:
    try:
        _, reason = _derivatives_adjust_shadow_source(symbol, score)
        return float(score), 'shadow-only|' + str(reason)
    except Exception:
        return float(score), 'shadow-only|derivatives-unavailable'


def _lane_enabled(lane: str) -> tuple[bool, str]:
    try:
        with open(LANE_HEALTH_PATH, 'r', encoding='utf-8') as f:
            row = json.load(f)
        h = ((row.get('lanes') or {}).get(lane) or {})
        until = float(h.get('disabled_until') or 0)
        if until > time.time():
            return True, f'shadow-lane-health-would-disable-until-{int(until)}'
    except Exception:
        pass
    return True, 'shadow-lane-health-observe-only'

'''
if 'shadow-lane-health-observe-only' not in s:
    if marker not in s:
        raise SystemExit('expert-v7: insertion marker missing')
    s = s.replace(marker, override + marker, 1)

startup_marker = "    print(f'[expert-system-v6] ONLINE daily_loss={EXPERT_DAILY_LOSS_CAP_USDT:.2f}USDT loss_streak={EXPERT_LOSS_STREAK_LIMIT}/{EXPERT_LOSS_STREAK_COOLOFF_SEC//3600}h drawdown={EXPERT_MAX_REALIZED_DRAWDOWN_USDT:.2f}USDT/{EXPERT_DRAWDOWN_COOLOFF_SEC//3600}h api_health={EXPERT_EXEC_HEALTH_STALE_SEC}s source=BINANCE_RECONCILED_PNL')\n"
startup = startup_marker + "    print('[expert-system-v7] ONLINE derivatives=SHADOW_ONLY lane_health=SHADOW_ONLY live_safety=RECONCILED_PNL_CIRCUITS')\n"
if '[expert-system-v7] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('expert-v7: v6 startup marker missing')
    s = s.replace(startup_marker, startup, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[expert-system-v7-patch] OK unvalidated derivatives score adjustments + counterfactual lane disable moved to shadow-only')
