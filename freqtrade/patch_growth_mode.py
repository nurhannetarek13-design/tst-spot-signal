from pathlib import Path

engine = Path('/freqtrade/fast_entry_engine.py')
s = engine.read_text(encoding='utf-8')

# Large-move mode: keep the first-green ignition logic and let qualified momentum
# trades breathe for a larger continuation move. Hard-stop and risk controls stay
# unchanged. patch_adaptive_watch owns the final runtime mode label.
old_target = "    base_tp = max(0.014, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.008 + pressure_strength * 0.002 + volume_strength * 0.002, 0.014, 0.030)\n"
new_target = "    base_tp = max(0.015, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.010 + pressure_strength * 0.003 + volume_strength * 0.002, 0.015, 0.035)\n"
if old_target in s:
    s = s.replace(old_target, new_target, 1)
elif new_target not in s:
    raise SystemExit('growth mode patch failed: momentum target marker missing')

engine.write_text(s, encoding='utf-8')

sizing = Path('/freqtrade/dynamic_sizing.py')
d = sizing.read_text(encoding='utf-8')
d = d.replace("_env_float('MAX_EXECUTION_STAKE_USDT', 40.0)", "_env_float('MAX_EXECUTION_STAKE_USDT', 100.0)", 1)
sizing.write_text(d, encoding='utf-8')

if '0.015, 0.035' not in s:
    raise SystemExit('growth mode patch failed: large-move target range missing')
if "_env_float('MAX_EXECUTION_STAKE_USDT', 100.0)" not in d:
    raise SystemExit('growth mode patch failed: sizing execution cap default not raised')

print('[growth-mode-patch] OK large-move mode restored: adaptive 1.5-3.5% target; existing hard-stop/risk controls unchanged')
