from pathlib import Path

p = Path("src/edge-worker.js")
s = p.read_text(encoding="utf-8")

# Telegram must receive actionable opportunities only. The legacy five-minute
# candidate digest is removed from canonical source, not merely skipped at runtime.
start = s.find("async function sendPeriodicScanDigest(env){")
if start != -1:
    end = s.find("async function scan(env,sendAlert){", start)
    if end == -1:
        raise SystemExit("scan() marker missing; refusing unsafe digest removal")
    s = s[:start] + s[end:]

# Scanner owns discovery/state only. buy-gateway owns the one user-facing
# Opportunity -> PREPARE -> CONFIRM BUY flow.
s = s.replace("return json(await scan(env,true));", "return json(await scan(env,false));")
s = s.replace(
    "await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,true);",
    "await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,false);",
)

if "sendPeriodicScanDigest" in s:
    raise SystemExit("legacy periodic digest still present")
if "scan(env,true)" in s:
    raise SystemExit("direct scanner Telegram alert path still present")
if "await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,false);" not in s:
    raise SystemExit("scheduled no-alert scanner path missing")

p.write_text(s, encoding="utf-8")
print("canonical runtime: actionable Telegram notifications only")
