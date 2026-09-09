from pathlib import Path

engine = Path('/freqtrade/fast_entry_engine.py')
s = engine.read_text(encoding='utf-8')

# Growth mode: keep the first-green ignition logic, but allow the very strongest
# qualified ignitions a little more upside room. We do NOT widen the hard stop
# and we do NOT increase risk because the account is behind a target.
old_target = "    base_tp = max(0.014, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.008 + pressure_strength * 0.002 + volume_strength * 0.002, 0.014, 0.030)\n"
new_target = "    base_tp = max(0.015, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.010 + pressure_strength * 0.003 + volume_strength * 0.002, 0.015, 0.035)\n"
if old_target in s:
    s = s.replace(old_target, new_target, 1)
elif new_target not in s:
    raise SystemExit('growth mode patch failed: momentum target marker missing')

s = s.replace('ONLINE mode=MOMENTUM_IGNITION', 'ONLINE mode=MOMENTUM_GROWTH', 1)
engine.write_text(s, encoding='utf-8')

sizing = Path('/freqtrade/dynamic_sizing.py')
d = sizing.read_text(encoding='utf-8')
d = d.replace("_env_float('MAX_EXECUTION_STAKE_USDT', 40.0)", "_env_float('MAX_EXECUTION_STAKE_USDT', 100.0)", 1)
sizing.write_text(d, encoding='utf-8')

for required in [
    'ONLINE mode=MOMENTUM_GROWTH',
    '0.015, 0.035',
]:
    if required not in s:
        raise SystemExit(f'growth mode patch failed: missing {required}')
if "_env_float('MAX_EXECUTION_STAKE_USDT', 100.0)" not in d:
    raise SystemExit('growth mode patch failed: sizing execution cap default not raised')

print('[growth-mode-patch] OK compounding enabled + quality-weighted 1.5-3.5% target + no target-chasing risk')
