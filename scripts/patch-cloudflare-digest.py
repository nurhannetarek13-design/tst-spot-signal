from pathlib import Path

p = Path("src/edge-worker.js")
s = p.read_text()

# User wants Telegram messages only for actionable trade opportunities.
# Keep scanning every minute, but remove the 5-minute candidate digest from the scheduled runtime.
old = "await monitorPaper(env); await sendPeriodicScanDigest(env); await scan(env,true);"
new = "await monitorPaper(env); await scan(env,true);"

if old in s:
    s = s.replace(old, new, 1)
elif new not in s:
    raise SystemExit("scheduled handler changed; refusing unsafe patch")

p.write_text(s)
