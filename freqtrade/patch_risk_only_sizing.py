from pathlib import Path

path = Path('/freqtrade/dynamic_sizing.py')
s = path.read_text(encoding='utf-8')

old = "    capital_fraction = _score_fraction(score, hard_fraction_cap)\n"
new = """    # The legacy Score/100 is heuristic, not a calibrated probability. Do not
    # let it increase real capital allocation. Until the EV model passes frozen
    # OOS/walk-forward validation, sizing is driven by stop-risk, available
    # balance and a conservative concentration ceiling only.
    uncalibrated_cap = min(max(_env_float('MAX_UNCALIBRATED_STAKE_FRACTION', 0.50), 0.05), 0.80)
    capital_fraction = min(hard_fraction_cap, uncalibrated_cap)
"""
if old in s:
    s = s.replace(old, new, 1)
elif 'MAX_UNCALIBRATED_STAKE_FRACTION' not in s:
    raise SystemExit('risk-only-sizing: capital fraction marker missing')

comment_old = """    # Stronger signals can use more capital, but actual dollars-at-risk remain
    # bounded by the stop, per-trade risk and daily-loss limits.
"""
comment_new = """    # Capital allocation must not rise merely because a heuristic score is high.
    # Dollars-at-risk remain bounded by the stop and hard risk limits.
"""
if comment_old in s:
    s = s.replace(comment_old, comment_new, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[risk-only-sizing-patch] OK heuristic score boost disabled; stop-risk + concentration cap control stake')
