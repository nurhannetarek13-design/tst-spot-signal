from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')
replacements = {
    "'/tmp/tst_candidate_events.jsonl'": "'/data/tst_candidate_events.jsonl'",
    "'/tmp/tst_lane_health.json'": "'/data/tst_lane_health.json'",
}
changed = 0
for old, new in replacements.items():
    if old in s:
        s = s.replace(old, new)
        changed += 1

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')

if '/tmp/tst_candidate_events.jsonl' in s or '/tmp/tst_lane_health.json' in s:
    raise SystemExit('persistent-outcome-paths: /tmp telemetry path remains')
print(f'[persistent-outcome-paths] OK changed={changed} candidate=/data lane_health=/data')
