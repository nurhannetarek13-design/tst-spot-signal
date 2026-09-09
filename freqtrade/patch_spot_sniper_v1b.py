from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'from pathlib import Path\n' not in s:
    marker = 'import time\n'
    if marker not in s:
        raise SystemExit('spot-sniper-v1b: import insertion marker missing')
    s = s.replace(marker, marker + 'from pathlib import Path\n', 1)

if 'Path(CANDIDATE_EVENT_PATH)' not in s:
    raise SystemExit('spot-sniper-v1b: candidate telemetry Path usage missing')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[spot-sniper-v1b-patch] OK candidate telemetry pathlib import fixed')
