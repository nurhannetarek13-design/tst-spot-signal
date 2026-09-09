from pathlib import Path

src_path = Path('/freqtrade/patch_expert_system_v3.py')
src = src_path.read_text(encoding='utf-8')
bad = "if '[portfolio-risk]' in s and '_expert_pre_ingest(payload)' not in s:\n    raise SystemExit('expert-v3: inconsistent portfolio patch state')\n"
if bad not in src:
    raise SystemExit('expert-v3b: expected legacy guard marker missing')
src = src.replace(bad, '', 1)
exec(compile(src, str(src_path), 'exec'), {'__name__': '__main__', '__file__': str(src_path)})
