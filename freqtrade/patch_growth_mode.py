from pathlib import Path

engine = Path('/freqtrade/fast_entry_engine.py')
s = engine.read_text(encoding='utf-8')

# Large-move mode may alter research/exit targets, never capital ceilings.
# Hard-stop, per-trade risk and execution-stake controls are owned by the safety
# layer and must remain independent from any growth objective.
old_target = "    base_tp = max(0.014, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.008 + pressure_strength * 0.002 + volume_strength * 0.002, 0.014, 0.030)\n"
new_target = "    base_tp = max(0.015, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.010 + pressure_strength * 0.003 + volume_strength * 0.002, 0.015, 0.035)\n"
if old_target in s:
    s = s.replace(old_target, new_target, 1)
elif new_target not in s:
    raise SystemExit('growth mode patch failed: momentum target marker missing')

engine.write_text(s, encoding='utf-8')

# Do not mutate dynamic_sizing.py here. In particular, never raise
# MAX_EXECUTION_STAKE_USDT as a side effect of a profit/growth mode.
sizing = Path('/freqtrade/dynamic_sizing.py')
d = sizing.read_text(encoding='utf-8')

if '0.015, 0.035' not in s:
    raise SystemExit('growth mode patch failed: large-move target range missing')
if "execution_cap = min(7.0" not in d:
    raise SystemExit('growth mode patch failed: hard execution ceiling missing')
if "max_risk = min(0.20" not in d:
    raise SystemExit('growth mode patch failed: hard risk ceiling missing')

print('[growth-mode-patch] OK adaptive 1.5-3.5% target; capital/risk ceilings remain safety-owned and unchanged')
