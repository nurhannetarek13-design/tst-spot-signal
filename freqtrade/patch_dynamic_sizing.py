from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text()

import_marker = 'import telegram_signal_bridge as bridge\n'
if 'import dynamic_sizing\n' not in s:
    if import_marker not in s:
        raise SystemExit('dynamic sizing patch failed: import marker missing')
    s = s.replace(import_marker, import_marker + 'import dynamic_sizing\n', 1)

payload_marker = "    payload = {\n        'id': f'{symbol}-{int(now)}',"
insert = (
    "    stake_usdt, free_usdt = dynamic_sizing.recommended_stake(sl_pct)\n"
    "    if stake_usdt is None:\n"
    "        print(f'[sizing] {symbol} blocked: free_usdt={free_usdt:.2f} below dynamic minimum/risk allowance')\n"
    "        return False\n"
    "    print(f'[sizing] {symbol} free_usdt={free_usdt:.2f} stake_usdt={stake_usdt:.2f} sl_pct={sl_pct*100:.2f}%')\n\n"
    + payload_marker
)
if '[sizing] {symbol} free_usdt=' not in s:
    if payload_marker not in s:
        raise SystemExit('dynamic sizing patch failed: live payload marker missing')
    s = s.replace(payload_marker, insert, 1)

live_tail = s.find(payload_marker)
if live_tail < 0:
    raise SystemExit('dynamic sizing patch failed: payload missing after insert')
idx = s.find("        'stakeUSDT': 5.5,", live_tail)
if idx < 0:
    if "        'stakeUSDT': stake_usdt," not in s[live_tail:]:
        raise SystemExit('dynamic sizing patch failed: live fixed stake missing')
else:
    s = s[:idx] + "        'stakeUSDT': stake_usdt," + s[idx + len("        'stakeUSDT': 5.5,"):]

path.write_text(s)
print('[dynamic-sizing-patch] OK preflight stake remains fixed; live stake is balance/risk aware')
