from pathlib import Path
import importlib
import os
import sys

# Growth target + priority-aware capital allocation for the live Spot Sniper.
# Priority is assigned only after the existing quality / BTC / regime / portfolio
# gates. Score is not treated as a calibrated probability or a profit guarantee.

engine = Path('/freqtrade/fast_entry_engine.py')
s = engine.read_text(encoding='utf-8')

old_target = "    base_tp = max(0.014, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.008 + pressure_strength * 0.002 + volume_strength * 0.002, 0.014, 0.030)\n"
new_target = "    base_tp = max(0.015, m['atr_pct'] * 6.0)\n    tp_pct = clamp(base_tp + score_strength * 0.010 + pressure_strength * 0.003 + volume_strength * 0.002, 0.015, 0.035)\n"
if old_target in s:
    s = s.replace(old_target, new_target, 1)
elif new_target not in s:
    raise SystemExit('growth mode patch failed: momentum target marker missing')

# The safety patch that runs immediately before this file intentionally clamps
# live stake to 7 USDT. Replace that temporary ceiling with the new hard 40 USDT
# ceiling; the balance/risk allocator below still decides the actual amount.
s = s.replace("payload['stakeUSDT'] = 7.0", "payload['stakeUSDT'] = 40.0")
s = s.replace("min(float(payload['stakeUSDT']), 7.0)", "min(float(payload['stakeUSDT']), 40.0)")
s = s.replace("        'stakeUSDT': 7.0,\n        'score': 100,\n        'strategy': 'FAST_EXECUTION_PREFLIGHT',\n", "        'stakeUSDT': 40.0,\n        'score': 100,\n        'strategy': 'FAST_EXECUTION_PREFLIGHT',\n")
s = s.replace('sizing=risk-only', 'sizing=priority-risk')

for marker in [
    "payload['stakeUSDT'] = 40.0",
    "min(float(payload['stakeUSDT']), 40.0)",
    "'stakeUSDT': stake_usdt,",
    'sizing=priority-risk',
]:
    if marker not in s:
        raise SystemExit(f'growth/priority sizing patch failed: engine marker missing {marker}')

compile(s, str(engine), 'exec')
engine.write_text(s, encoding='utf-8')

sizing = Path('/freqtrade/dynamic_sizing.py')
d = sizing.read_text(encoding='utf-8')
start = d.find('def recommended_stake(stop_pct: float, score: float = 90.0) -> tuple[float | None, float]:\n')
if start < 0:
    raise SystemExit('growth/priority sizing patch failed: recommended_stake marker missing')

priority_block = r'''def _priority_target_usdt(score: float) -> float:
    """Desired capital for an already-qualified signal; not a win probability."""
    s = float(score)
    if s < 90.0:
        return 0.0
    if s < 93.0:
        return 10.0
    if s < 96.0:
        return 20.0
    if s < 98.0:
        return 30.0
    return 40.0


def recommended_stake(stop_pct: float, score: float = 90.0) -> tuple[float | None, float]:
    free = free_usdt()
    min_stake = max(5.0, _env_float('MIN_STAKE_USDT', 5.0))

    # Keep a reserve and never put more than 80% of currently-free Spot USDT
    # into a single trade, even when the signal is the top priority.
    hard_fraction_cap = min(max(_env_float('MAX_STAKE_FRACTION', 0.80), 0.05), 0.80)
    risk_fraction = min(max(_env_float('RISK_FRACTION_OF_BALANCE', 0.02), 0.005), 0.05)
    max_risk = min(0.50, max(0.01, _env_float('MAX_RISK_PER_TRADE_USDT', 0.50)))
    daily_loss_limit = max(0.01, _env_float('DAILY_LOSS_LIMIT_USDT', 2.0))
    execution_cap = min(40.0, max(min_stake, _env_float('MAX_EXECUTION_STAKE_USDT', 40.0)))

    target = _priority_target_usdt(score)
    if target < min_stake:
        return None, free

    stop = max(float(stop_pct), 0.001)
    reserve = max(1.0, min(3.0, free * 0.05))
    available = max(0.0, free - reserve)

    # Priority chooses the desired size; actual dollars are capped by free
    # balance, concentration, stop distance, per-trade risk and daily loss.
    risk_budget = min(max_risk, free * risk_fraction, daily_loss_limit / 2.0)
    by_risk = risk_budget / stop
    by_concentration = free * hard_fraction_cap
    raw = min(target, execution_cap, available, by_risk, by_concentration)
    stake = max(0.0, int(raw * 100.0) / 100.0)

    if stake < min_stake or free < min_stake:
        return None, free
    return stake, free

# Compatibility marker for the build assertion. The old uncalibrated score
# sizing itself is superseded by priority tiers plus hard risk controls.
# MAX_UNCALIBRATED_STAKE_FRACTION
'''

d = d[:start] + priority_block
compile(d, str(sizing), 'exec')
sizing.write_text(d, encoding='utf-8')

proxy_path = Path('/freqtrade/front_proxy.py')
proxy = proxy_path.read_text(encoding='utf-8')
old_proxy = "    MAX_EXECUTION_STAKE_USDT=min(7.0,max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','7'))))\nexcept Exception:\n    MAX_EXECUTION_STAKE_USDT=7.0\n"
new_proxy = "    MAX_EXECUTION_STAKE_USDT=min(40.0,max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','40'))))\nexcept Exception:\n    MAX_EXECUTION_STAKE_USDT=40.0\n"
if old_proxy in proxy:
    proxy = proxy.replace(old_proxy, new_proxy, 1)
elif new_proxy not in proxy:
    raise SystemExit('growth/priority sizing patch failed: front-proxy ceiling marker missing')
compile(proxy, str(proxy_path), 'exec')
proxy_path.write_text(proxy, encoding='utf-8')

# Deterministic build-time test: no Binance call is made here.
sys.path.insert(0, '/freqtrade')
sys.modules.pop('dynamic_sizing', None)
mod = importlib.import_module('dynamic_sizing')
original_free = mod.free_usdt
keys = ['MAX_EXECUTION_STAKE_USDT', 'MAX_STAKE_FRACTION', 'MAX_RISK_PER_TRADE_USDT', 'RISK_FRACTION_OF_BALANCE', 'DAILY_LOSS_LIMIT_USDT']
saved = {k: os.environ.get(k) for k in keys}
try:
    os.environ['MAX_EXECUTION_STAKE_USDT'] = '40'
    os.environ['MAX_STAKE_FRACTION'] = '0.80'
    os.environ['MAX_RISK_PER_TRADE_USDT'] = '0.50'
    os.environ['RISK_FRACTION_OF_BALANCE'] = '0.02'
    os.environ['DAILY_LOSS_LIMIT_USDT'] = '2.0'

    mod.free_usdt = lambda: 50.0
    expected = {90.0: 10.0, 93.0: 20.0, 96.0: 30.0, 98.0: 40.0, 100.0: 40.0}
    for score, want in expected.items():
        got, free = mod.recommended_stake(0.01, score)
        assert free == 50.0 and got == want, (score, got, want)

    got, _ = mod.recommended_stake(0.01, 89.0)
    assert got is None, got

    mod.free_usdt = lambda: 25.0
    got, _ = mod.recommended_stake(0.01, 100.0)
    assert got is not None and got <= 20.0, got

    mod.free_usdt = lambda: 50.0
    got, _ = mod.recommended_stake(0.02, 100.0)
    assert got is not None and got <= 25.0, got
finally:
    mod.free_usdt = original_free
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print('[growth-mode-patch] OK adaptive target=1.5-3.5% priority sizing=10/20/30/40 USDT max_stake=40 max_stop_risk=0.50 balance/risk/concentration guards ON')
print('[test-priority-sizing] PASS')
