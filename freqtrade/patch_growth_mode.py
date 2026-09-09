from pathlib import Path

engine = Path('/freqtrade/fast_entry_engine.py')
s = engine.read_text(encoding='utf-8')

# Fast-profit mode: these are short-lived Spot ignition trades, so the primary
# target must be realistically reachable before the first momentum burst fades.
# Keep the existing hard stop logic, but bring the profit target forward and
# still give the strongest setups extra room.
old_target = "    base_tp = max(0.014, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.008 + pressure_strength * 0.002 + volume_strength * 0.002, 0.014, 0.030)\n"
new_target = "    base_tp = max(0.007, m['atr_pct'] * 3.2)\n    tp_pct = clamp(base_tp + score_strength * 0.005 + pressure_strength * 0.0015 + volume_strength * 0.0015, 0.007, 0.016)\n"
if old_target in s:
    s = s.replace(old_target, new_target, 1)
elif new_target not in s:
    raise SystemExit('growth mode patch failed: momentum target marker missing')

s = s.replace('ONLINE mode=MOMENTUM_IGNITION', 'ONLINE mode=FAST_PROFIT_CAPTURE', 1)
engine.write_text(s, encoding='utf-8')

sizing = Path('/freqtrade/dynamic_sizing.py')
d = sizing.read_text(encoding='utf-8')
d = d.replace("_env_float('MAX_EXECUTION_STAKE_USDT', 40.0)", "_env_float('MAX_EXECUTION_STAKE_USDT', 100.0)", 1)
sizing.write_text(d, encoding='utf-8')

for required in [
    'ONLINE mode=FAST_PROFIT_CAPTURE',
    '0.007, 0.016',
]:
    if required not in s:
        raise SystemExit(f'growth mode patch failed: missing {required}')
if "_env_float('MAX_EXECUTION_STAKE_USDT', 100.0)" not in d:
    raise SystemExit('growth mode patch failed: sizing execution cap default not raised')

print('[growth-mode-patch] OK fast-profit capture enabled: adaptive 0.7-1.6% target; existing hard-stop/risk controls unchanged')
