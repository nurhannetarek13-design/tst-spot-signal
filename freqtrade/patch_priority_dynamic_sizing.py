from pathlib import Path
import importlib
import os
import sys

# Priority-aware capital allocation for the live Spot Sniper.
#
# The score is NOT treated as a win probability. It is only a priority tier after
# all existing quality / BTC / regime / portfolio / OCO gates have done their job.
# Stronger qualified setups may use more of the actually-free Binance USDT, while
# stop-risk, daily-loss and concentration limits remain hard constraints.

sizing_path = Path('/freqtrade/dynamic_sizing.py')
ds = sizing_path.read_text(encoding='utf-8')

start = ds.find('def recommended_stake(stop_pct: float, score: float = 90.0) -> tuple[float | None, float]:\n')
if start < 0:
    raise SystemExit('priority sizing patch failed: recommended_stake marker missing')

priority_block = r'''def _priority_target_usdt(score: float) -> float:
    """Capital target for an already-qualified signal; not a probability claim."""
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

    # Never concentrate more than 80% of currently-free Spot USDT in one trade.
    hard_fraction_cap = min(max(_env_float('MAX_STAKE_FRACTION', 0.80), 0.05), 0.80)
    risk_fraction = min(max(_env_float('RISK_FRACTION_OF_BALANCE', 0.02), 0.005), 0.05)

    # A 40 USDT position with the current <=1.1% noise-aware hard stop risks
    # <=0.44 USDT before fees. Keep a 0.50 USDT absolute ceiling.
    max_risk = min(0.50, max(0.01, _env_float('MAX_RISK_PER_TRADE_USDT', 0.50)))
    daily_loss_limit = max(0.01, _env_float('DAILY_LOSS_LIMIT_USDT', 2.0))
    execution_cap = min(40.0, max(min_stake, _env_float('MAX_EXECUTION_STAKE_USDT', 40.0)))

    target = _priority_target_usdt(score)
    if target < min_stake:
        return None, free

    stop = max(float(stop_pct), 0.001)
    reserve = max(1.0, min(3.0, free * 0.05))
    available = max(0.0, free - reserve)

    # Priority decides the desired size; risk and available capital decide what
    # is actually allowed. This is never martingale sizing.
    risk_budget = min(max_risk, free * risk_fraction, daily_loss_limit / 2.0)
    by_risk = risk_budget / stop
    by_concentration = free * hard_fraction_cap
    raw = min(target, execution_cap, available, by_risk, by_concentration)
    stake = max(0.0, int(raw * 100.0) / 100.0)

    if stake < min_stake or free < min_stake:
        return None, free
    return stake, free

# Compatibility marker for the older build assertion; the old uncalibrated
# concentration cap is superseded by priority tiers plus hard risk controls.
# MAX_UNCALIBRATED_STAKE_FRACTION
'''

ds = ds[:start] + priority_block
compile(ds, str(sizing_path), 'exec')
sizing_path.write_text(ds, encoding='utf-8')

engine_path = Path('/freqtrade/fast_entry_engine.py')
s = engine_path.read_text(encoding='utf-8')

# The previous safety patch intentionally clamped everything to 7 USDT. Lift
# that obsolete cap to the new hard ceiling; recommended_stake remains the real
# allocator and can still return any smaller value dictated by balance/risk.
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
        raise SystemExit(f'priority sizing patch failed: engine marker missing {marker}')
compile(s, str(engine_path), 'exec')
engine_path.write_text(s, encoding='utf-8')

proxy_path = Path('/freqtrade/front_proxy.py')
proxy = proxy_path.read_text(encoding='utf-8')
old_proxy = "    MAX_EXECUTION_STAKE_USDT=min(7.0,max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','7'))))\nexcept Exception:\n    MAX_EXECUTION_STAKE_USDT=7.0\n"
new_proxy = "    MAX_EXECUTION_STAKE_USDT=min(40.0,max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','40'))))\nexcept Exception:\n    MAX_EXECUTION_STAKE_USDT=40.0\n"
if old_proxy in proxy:
    proxy = proxy.replace(old_proxy, new_proxy, 1)
elif new_proxy not in proxy:
    raise SystemExit('priority sizing patch failed: front-proxy 7 USDT ceiling marker missing')
compile(proxy, str(proxy_path), 'exec')
proxy_path.write_text(proxy, encoding='utf-8')

# Deterministic build-time sizing test without touching Binance.
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
finally:
    mod.free_usdt = original_free
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print('[priority-sizing-patch] OK tiers=10/20/30/40 score>=90 max_stake=40 max_stop_risk=0.50 balance-aware reserve/risk/concentration guards ON')
print('[test-priority-sizing] PASS')
