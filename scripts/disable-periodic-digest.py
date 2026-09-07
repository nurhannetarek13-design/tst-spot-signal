from pathlib import Path

p = Path('src/edge-worker.js')
s = p.read_text(encoding='utf-8')
old = 'await monitorUnifiedDerivative(env); await monitorPaper(env); await sendPeriodicScanDigest(env); await scan(env,true);'
new = 'await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,true);'

if old in s:
    s = s.replace(old, new, 1)
elif new not in s:
    raise SystemExit('scheduled handler changed; refusing unsafe patch')

p.write_text(s, encoding='utf-8')
print('periodic scan digest disabled for deployed runtime')
