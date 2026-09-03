#!/usr/bin/env python3
import datetime as dt
import json
import pathlib

MANIFEST=pathlib.Path("validation/fusion/candidate-manifest.json")
GATE=pathlib.Path("validation/fusion/gate-latest.json")
REJECTED=pathlib.Path("validation/fusion/rejected-candidates.json")
FORWARD=pathlib.Path("validation/fusion/forward-latest.json")
LEDGER=pathlib.Path("paper/public-edge-forward-ledger.json")

def load(p,default):
    try:return json.loads(p.read_text())
    except:return default

m=load(MANIFEST,{})
g=load(GATE,{})
r=load(REJECTED,{"rejected":[],"ttlDays":7})
fp=m.get("candidateFingerprint")
if not fp or g.get("candidateFingerprint")!=fp or g.get("liveReady") is True:
    print(json.dumps({"changed":False,"reason":"NO_ROTATION_NEEDED"}))
    raise SystemExit(0)

summary=g.get("validatorSummary") or {}
hard_fail=[]
for name in ["vectorbt","freqtrade","jesse","nautilus"]:
    v=summary.get(name) or {}
    if v.get("candidateFingerprint")!=fp: continue
    trades=int(v.get("trades") or 0)
    # Reject on actual stressed economic failure, not merely on insufficient sample size.
    indep=v.get("independentEnginePass")
    stress_exp=v.get("stressExpectancyUSDT")
    stress_pf=v.get("stressProfitFactor")
    economic_fail=(
        trades>=10
        and stress_exp is not None
        and stress_pf is not None
        and (float(stress_exp)<0 or float(stress_pf)<0.95)
    )
    if economic_fail:
        hard_fail.append(name)
    elif trades>=30 and indep is False and stress_exp is not None and float(stress_exp)<=0:
        hard_fail.append(name)
    elif trades>=100 and indep is None and v.get("pass") is False:
        hard_fail.append(name)

if len(hard_fail)<2:
    print(json.dumps({"changed":False,"reason":"INSUFFICIENT_HARD_FAILURES","hardFail":hard_fail}))
    raise SystemExit(0)

now=dt.datetime.now(dt.timezone.utc).isoformat()
rows=list(r.get("rejected") or [])
if not any(x.get("candidateFingerprint")==fp for x in rows):
    rows.append({
      "candidateId":m.get("candidateId"),
      "candidateFingerprint":fp,
      "symbol":m.get("symbol"),
      "family":m.get("family"),
      "hardFailValidators":hard_fail,
      "rejectedAt":now,
    })
r["rejected"]=rows[-100:]
REJECTED.write_text(json.dumps(r,indent=2))

ttl=int(r.get("ttlDays") or 7)
cut=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=ttl)
active=set()
for x in r["rejected"]:
    try:
        ts=dt.datetime.fromisoformat(str(x["rejectedAt"]).replace("Z","+00:00"))
        if ts>=cut: active.add(x.get("candidateFingerprint"))
    except: pass

pool=m.get("candidatePool") or []
next_c=next((x for x in pool if x.get("candidateFingerprint") not in active),None)
if not next_c:
    new={
      "strategyId":"TST_UNIFIED_CANDIDATE_V1","candidateId":None,"candidateFingerprint":None,
      "status":"POOL_EXHAUSTED_AFTER_VALIDATOR_REJECTIONS",
      "candidatePool":pool,"poolSize":len(pool),
      "recentRejectedFingerprints":sorted(active),
      "authorization":"RESEARCH_ONLY","liveTrading":False,
      "validatorsRequired":["vectorbt","freqtrade","jesse","nautilus","forward"],
      "generatedAt":now,
    }
else:
    new={
      "strategyId":"TST_UNIFIED_CANDIDATE_V1",**next_c,
      "candidatePool":pool,"poolSize":len(pool),
      "recentRejectedFingerprints":sorted(active),
      "authorization":"VALIDATION_AND_FORWARD_PAPER_ONLY","liveTrading":False,
      "validatorsRequired":["vectorbt","freqtrade","jesse","nautilus","forward"],
      "generatedAt":now,
      "rotationReason":{"rejectedFingerprint":fp,"hardFailValidators":hard_fail},
    }
MANIFEST.write_text(json.dumps(new,indent=2))

# Candidate rotation must also reset forward state immediately. Rotation commits use
# [skip ci], so we cannot rely on a downstream push-trigger to clear a stale paper candidate.
forward={
  "engine":"FORWARD_PAPER",
  "strategyId":"TST_UNIFIED_FORWARD_V1",
  "status":"RESET_FOR_NEW_CANDIDATE" if new.get("candidateFingerprint") else "NO_CANDIDATE",
  "pass":False,
  "candidateId":new.get("candidateId"),
  "candidateFingerprint":new.get("candidateFingerprint"),
  "metrics":{"trades":0,"wins":0,"winRate":0,"netPnlUSDT":0,"expectancyUSDT":0,"profitFactor":0,"maxDrawdownUSDT":0},
  "open":None,
  "authorization":"FORWARD_PAPER_ONLY",
  "liveTrading":False,
  "generatedAt":now,
  "notes":"Forward state reset atomically with candidate rotation; no cancelled paper position is counted as a trade."
}
FORWARD.parent.mkdir(parents=True,exist_ok=True)
FORWARD.write_text(json.dumps(forward,indent=2))

ledger=load(LEDGER,{"open":None,"closed":[],"seen":{},"cancelled":[]})
if ledger.get("open"):
    cancelled=list(ledger.get("cancelled") or [])
    cancelled.append({
      **ledger["open"],
      "cancelledAt":now,
      "reason":"UNIFIED_CANDIDATE_REJECTED_OR_ROTATED",
      "countedAsTrade":False,
    })
    ledger["cancelled"]=cancelled[-100:]
ledger["open"]=None
LEDGER.parent.mkdir(parents=True,exist_ok=True)
LEDGER.write_text(json.dumps(ledger,indent=2))

print(json.dumps({"changed":True,"rejected":fp,"hardFail":hard_fail,"next":new.get("candidateId"),"forwardReset":True},indent=2))
