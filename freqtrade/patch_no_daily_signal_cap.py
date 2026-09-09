from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

old_gate = "    if signals_today >= MAX_SIGNALS_PER_DAY:\n        return False\n"
new_gate = "    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY:\n        return False\n"
if old_gate in s:
    s = s.replace(old_gate, new_gate, 1)
elif new_gate not in s:
    raise SystemExit('no-daily-cap patch failed: signal-count gate marker missing')

# Make the runtime startup line explicit when 0 means unlimited.
old_log = "f'[fast-engine] ONLINE mode=ADAPTIVE_WATCH watch={WATCH_SCORE:.0f} direct={DIRECT_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '"
new_log = "f'[fast-engine] ONLINE mode=ADAPTIVE_WATCH watch={WATCH_SCORE:.0f} direct={DIRECT_SCORE:.0f} max/day={\"UNLIMITED\" if MAX_SIGNALS_PER_DAY <= 0 else MAX_SIGNALS_PER_DAY} '"
if old_log in s:
    s = s.replace(old_log, new_log, 1)
elif 'max/day={"UNLIMITED" if MAX_SIGNALS_PER_DAY <= 0 else MAX_SIGNALS_PER_DAY}' not in s:
    raise SystemExit('no-daily-cap patch failed: startup log marker missing')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[no-daily-signal-cap] OK FAST_MAX_SIGNALS_PER_DAY<=0 means unlimited; quality/risk gates unchanged')
