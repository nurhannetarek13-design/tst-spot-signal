#!/usr/bin/env python3
import json
import os
from pathlib import Path

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
CANDIDATE_PATHS = [
    Path("validation/candidate-manifest.json"),
    Path("validation/fusion/candidate-manifest.json"),
    Path("fusion/candidate-manifest.json"),
]

def load_candidate():
    for p in CANDIDATE_PATHS:
        if p.exists():
            try:
                x=json.load(open(p))
                return str(p), x
            except Exception:
                return str(p), None
    return None, None

path,candidate=load_candidate()
reasons=[]
if candidate is None:
    reasons.append("NO_PRIMARY_EDGE_CANDIDATE")
else:
    # Deliberately accept only an explicit edge-pass flag. Historical framework pass is insufficient.
    edge_pass = bool(candidate.get("primaryEdgePass") or candidate.get("passPrimaryEdgeGate"))
    rejected = str(candidate.get("status", "")).upper().startswith("REJECT")
    if not edge_pass or rejected:
        reasons.append("PRIMARY_EDGE_GATE_NOT_PASSED")

has_key=bool(os.getenv("TARDIS_KEY") or os.getenv("TARDIS_API_KEY"))
if not has_key:
    reasons.append("NO_TARDIS_CREDENTIAL_IN_CI")

out={
  "engine":"HFTBACKTEST_EXECUTION_PREFLIGHT_V1",
  "status":"READY_FOR_CANONICAL_REPLAY" if not reasons else "BLOCKED_FAIL_CLOSED",
  "ready":not reasons,
  "candidatePath":path,
  "symbols":SYMBOLS,
  "reasons":reasons,
  "authorization":"RESEARCH_ONLY",
  "liveTrading":False,
  "executorAllowed":False,
  "meaning":"A blocked result is expected when no independently proven primary edge or canonical Tardis access is present."
}
Path("validation/execution").mkdir(parents=True,exist_ok=True)
json.dump(out,open("validation/execution/preflight-latest.json","w"),indent=2)
print(json.dumps(out,indent=2))
