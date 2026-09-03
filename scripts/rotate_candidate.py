#!/usr/bin/env python3
import datetime as dt
import json
import pathlib

MANIFEST=pathlib.Path("validation/fusion/candidate-manifest.json")
GATE=pathlib.Path("validation/fusion/gate-latest.json")
REJECTED=pathlib.Path("validation/fusion/rejected-candidates.json")

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
    # Gate summary is enough to know pass/fail and sample size. Require 30+ trades
    # before treating a historical validator failure as a hard rejection.
    if trades>=30 and v.get("pass") is False:
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
print(json.dumps({"changed":True,"rejected":fp,"hardFail":hard_fail,"next":new.get("candidateId")},indent=2))
