from pathlib import Path

p = Path("src/edge-worker.js")
s = p.read_text()

canonical = '  "https://api.binance.com",\n'
if canonical not in s:
    marker = 'const API_BASES = [\n'
    if marker not in s:
        raise SystemExit("API_BASES marker missing; refusing unsafe patch")
    s = s.replace(marker, marker + canonical, 1)

p.write_text(s)
