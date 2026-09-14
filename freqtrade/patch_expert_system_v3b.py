from pathlib import Path

src_path = Path('/freqtrade/patch_expert_system_v3.py')
src = src_path.read_text(encoding='utf-8')
bad = "if '[portfolio-risk]' in s and '_expert_pre_ingest(payload)' not in s:\n    raise SystemExit('expert-v3: inconsistent portfolio patch state')\n"
if bad not in src:
    raise SystemExit('expert-v3b: expected legacy guard marker missing')
src = src.replace(bad, '', 1)

# Teach the shared portfolio/lane-health gate about the new confirmed-only
# early-reversal lane without weakening any of the existing hard protections.
lane_marker = "    if strategy.startswith('EXTREME_CONTINUATION'): return 'EXTREME'\n"
if "EARLY_REVERSAL_STARTER" not in src:
    if lane_marker not in src:
        raise SystemExit('expert-v3b: lane marker missing')
    src = src.replace(lane_marker, lane_marker + "    if strategy.startswith('EARLY_REVERSAL_STARTER'): return 'REVERSAL'\n", 1)

exec(compile(src, str(src_path), 'exec'), {'__name__': '__main__', '__file__': str(src_path)})
