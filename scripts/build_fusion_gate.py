#!/usr/bin/env python3
import datetime as dt
import json
import pathlib

ROOT=pathlib.Path("validation/fusion")
OUT=ROOT/"gate-latest.json"
MANIFEST=ROOT/"candidate-manifest.json"

FILES={
  "vectorbt": ROOT/"vectorbt-candidate-latest.json",
  "freqtrade": ROOT/"freqtrade-latest.json",
  "jesse": ROOT/"jesse-latest.json",
  "nautilus": ROOT/"nautilus-latest.json",
  "forward": ROOT/"forward-latest.json",
}
EXPECTED={
  "vectorbt":"TST_CANDIDATE_VECTORBT_VALIDATOR_V1",
  "freqtrade":"TST_CANDIDATE_FREQTRADE_VALIDATOR_V1",
  "jesse":"TST_CANDIDATE_JESSE_VALIDATOR_V1",
  "nautilus":"TST_CANDIDATE_NAUTILUS_VALIDATOR_V1",
  "forward":"TST_UNIFIED_FORWARD_V1",
}

def load(p):
    try:return json.loads(p.read_text())
    except Exception:return None

m=load(MANIFEST) or {}
fp=m.get("candidateFingerprint")
reasons=[]
validators={}

if not fp:
    reasons.append("CANDIDATE:MISSING")

for name,p in FILES.items():
    row=load(p)
    if not row:
        validators[name]={"status":"MISSING","pass":False}
        reasons.append(f"{name}:MISSING")
        continue
    validators[name]=row
    if row.get("strategyId")!=EXPECTED[name]:
        reasons.append(f"{name}:STRATEGY_ID_MISMATCH")
    if fp and row.get("candidateFingerprint")!=fp:
        reasons.append(f"{name}:CANDIDATE_MISMATCH")
    if row.get("pass") is not True:
        reasons.append(f"{name}:FAIL")
    if name=="forward":
        n=int((row.get("metrics") or {}).get("trades") or 0)
        if n<50:reasons.append("forward:LT_50_FORWARD_TRADES")
    else:
        n=int((row.get("base") or {}).get("trades") or 0)
        if n<100:reasons.append(f"{name}:LT_100_TRADES")

report={
  "engine":"UNIFIED_FUSION_GATE",
  "strategyId":"TST_UNIFIED_CANDIDATE_V1",
  "candidateId":m.get("candidateId"),
  "candidateFingerprint":fp,
  "symbol":m.get("symbol"),
  "family":m.get("family"),
  "liveReady":len(reasons)==0,
  "liveTrading":False,
  "executorAutoEnable":False,
  "reasons":list(dict.fromkeys(reasons)),
  "validatorSummary":{
      k:{
          "status":v.get("status"),
          "pass":bool(v.get("pass")),
          "candidateFingerprint":v.get("candidateFingerprint"),
          "trades":int(((v.get("metrics") if k=="forward" else v.get("base")) or {}).get("trades") or 0),
      } for k,v in validators.items()
  },
  "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
  "note":"Live can only be considered after this gate is ready, and still requires explicit user enable. This file never enables trading."
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
