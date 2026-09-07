from pathlib import Path

p = Path('src/edge-worker.js')
s = p.read_text(encoding='utf-8')
old = 'await monitorUnifiedDerivative(env); await monitorPaper(env); await sendPeriodicScanDigest(env); await scan(env,true);'
new = 'await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,true);'
if old not in s:
    raise SystemExit('scheduled periodic digest call not found')
p.write_text(s.replace(old, new, 1), encoding='utf-8')
print('periodic scan digest disabled for deployed runtime')
