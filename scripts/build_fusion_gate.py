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
cid=m.get("candidateId")
fp=m.get("candidateFingerprint")
validation=m.get("validation") or {}
historical_scope=validation.get("historicalScope","FULL_CANDIDATE")
forward_scope=validation.get("forwardScope","FULL_CANDIDATE")
required=list(m.get("validatorsRequired") or FILES.keys())
historical_required=list(validation.get("historicalValidators") or [x for x in required if x!="forward"])
forward_required=list(validation.get("forwardValidators") or (["forward"] if "forward" in required else []))
reasons=[]
validators={}

if not cid or not fp:
    reasons.append("CANDIDATE:MISSING")

for name,p in FILES.items():
    row=load(p)
    if not row:
        validators[name]={"status":"MISSING","pass":False,"candidateMatch":False}
        if name in required:reasons.append(f"{name}:MISSING")
        continue
    row=dict(row)
    id_match=bool(cid) and row.get("candidateId")==cid
    fp_match=bool(fp) and row.get("candidateFingerprint")==fp
    candidate_match=id_match and fp_match
    row["candidateIdMatch"]=id_match
    row["candidateFingerprintMatch"]=fp_match
    row["candidateMatch"]=candidate_match
    validators[name]=row
    if name not in required:
        continue
    if row.get("strategyId")!=EXPECTED[name]:
        reasons.append(f"{name}:STRATEGY_ID_MISMATCH")
    if not id_match:
        reasons.append(f"{name}:CANDIDATE_ID_MISMATCH")
    if not fp_match:
        reasons.append(f"{name}:CANDIDATE_FINGERPRINT_MISMATCH")
    if row.get("pass") is not True:
        reasons.append(f"{name}:FAIL")
    if name=="forward":
        n=int((row.get("metrics") or {}).get("trades") or 0)
        if n<50:reasons.append("forward:LT_50_FORWARD_TRADES")
    else:
        n=int((row.get("base") or {}).get("trades") or 0)
        if n<100:reasons.append(f"{name}:LT_100_TRADES")

# Historical CORE_TRIGGER_ONLY can only advance to forward-paper collection.
# It is never equivalent to validating the complete L2 strategy.
core_only=(historical_scope=="CORE_TRIGGER_ONLY")
if core_only:
    reasons.append("FULL_STRATEGY_L2:NOT_HISTORICALLY_VALIDATED")

# Full production-review gate remains intentionally strict and fail-closed.
full_ready=(len(reasons)==0) and not core_only

small_reasons=[]
if not cid or not fp:
    small_reasons.append("CANDIDATE:MISSING")
if core_only:
    small_reasons.append("CORE_TRIGGER_ONLY:CANNOT_AUTHORIZE_SMALL_LIVE_REVIEW")
    small_reasons.append("FULL_STRATEGY_L2:FORWARD_VALIDATION_REQUIRED")

independent_pass_count=0
for name in historical_required:
    v=validators.get(name) or {}
    if v.get("strategyId")!=EXPECTED.get(name):
        small_reasons.append(f"{name}:STRATEGY_ID_MISMATCH")
    if v.get("candidateId")!=cid:
        small_reasons.append(f"{name}:CANDIDATE_ID_MISMATCH")
    if v.get("candidateFingerprint")!=fp:
        small_reasons.append(f"{name}:CANDIDATE_FINGERPRINT_MISMATCH")
    if v.get("candidateMatch") is not True:
        continue
    if core_only and v.get("validationScope") not in ("CORE_TRIGGER_ONLY",None):
        small_reasons.append(f"{name}:VALIDATION_SCOPE_MISMATCH")
    base=v.get("base") or {}
    stress=v.get("stress2x") or {}
    n=int(base.get("trades") or 0)
    bpf=float(base.get("profitFactor") or 0)
    be=float(base.get("expectancyUSDT") or 0)
    spf=float(stress.get("profitFactor") or 0)
    se=float(stress.get("expectancyUSDT") or 0)
    if n<30:
        small_reasons.append(f"{name}:LT_30_TRADES")
    if bpf<1.15:
        small_reasons.append(f"{name}:BASE_PF_LT_1_15")
    if be<=0:
        small_reasons.append(f"{name}:BASE_EXPECTANCY_NONPOSITIVE")
    if spf<1.0:
        small_reasons.append(f"{name}:STRESS_PF_LT_1_0")
    if se<=0:
        small_reasons.append(f"{name}:STRESS_EXPECTANCY_NONPOSITIVE")
    if v.get("independentEnginePass") is True:
        independent_pass_count+=1

min_independent=min(3,max(1,len(historical_required))) if historical_required else 0
if independent_pass_count<min_independent:
    small_reasons.append(f"HISTORICAL:LT_{min_independent}_INDEPENDENT_ENGINE_PASSES")

# Even a perfect CORE-only history is merely permission to collect full forward evidence.
small_ready=(len(small_reasons)==0) and not core_only

historical_core_pass=bool(historical_required) and all(
    (validators.get(name) or {}).get("candidateMatch") is True
    and (validators.get(name) or {}).get("pass") is True
    for name in historical_required
)
forward_full_pass=bool(forward_required) and all(
    (validators.get(name) or {}).get("candidateMatch") is True
    and (validators.get(name) or {}).get("pass") is True
    for name in forward_required
)

report={
  "engine":"UNIFIED_FUSION_GATE",
  "strategyId":"TST_UNIFIED_CANDIDATE_V1",
  "candidateId":cid,
  "candidateFingerprint":fp,
  "symbol":m.get("symbol"),
  "family":m.get("family"),
  "historicalScope":historical_scope,
  "forwardScope":forward_scope,
  "requiredValidators":required,
  "historicalValidatorsRequired":historical_required,
  "forwardValidatorsRequired":forward_required,
  "historicalCorePass":historical_core_pass,
  "fullStrategyValidated":forward_full_pass if core_only else full_ready,
  "eligibleForForwardPaperCollection":historical_core_pass if core_only else False,
  "allValidatorsCurrentCandidate":bool(cid and fp) and all((validators.get(k) or {}).get("candidateMatch") is True for k in required),
  "liveReady":full_ready,
  "smallLiveReviewReady":small_ready,
  "smallLiveReviewReasons":list(dict.fromkeys(small_reasons)),
  "smallLiveReviewPolicy":{
      "purpose":"Manual review only after the complete strategy scope is independently validated; never auto-enables execution.",
      "historicalValidatorsRequired":historical_required,
      "sameCandidateIdAndFingerprintRequired":True,
      "minTradesPerEngine":30,
      "minIndependentEnginePasses":min_independent,
      "baseProfitFactorMin":1.15,
      "baseExpectancyPositive":True,
      "stressProfitFactorMin":1.0,
      "stressExpectancyPositive":True,
      "forwardTradesRequired":50 if core_only else 0,
      "coreTriggerOnlyCannotAuthorizeLive":True,
      "suggestedMaxPositionUSDT":7,
      "suggestedMaxConcurrentPositions":1,
      "suggestedDailyLossCapUSDT":0.5
  },
  "liveTrading":False,
  "executorAutoEnable":False,
  "reasons":list(dict.fromkeys(reasons)),
  "validatorSummary":{
      k:{
          "status":v.get("status"),
          "pass":bool(v.get("pass")),
          "validationScope":v.get("validationScope"),
          "fullStrategyValidated":v.get("fullStrategyValidated"),
          "candidateId":v.get("candidateId"),
          "candidateFingerprint":v.get("candidateFingerprint"),
          "candidateIdMatch":v.get("candidateIdMatch"),
          "candidateFingerprintMatch":v.get("candidateFingerprintMatch"),
          "candidateMatch":v.get("candidateMatch"),
          "trades":int(((v.get("metrics") if k=="forward" else v.get("base")) or {}).get("trades") or 0),
          "independentEnginePass":v.get("independentEnginePass"),
          "baseProfitFactor":float(((v.get("base") or {}).get("profitFactor") or 0)) if k!="forward" else None,
          "baseExpectancyUSDT":float(((v.get("base") or {}).get("expectancyUSDT") or 0)) if k!="forward" else None,
          "stressProfitFactor":float(((v.get("stress2x") or {}).get("profitFactor") or 0)) if k!="forward" else None,
          "stressExpectancyUSDT":float(((v.get("stress2x") or {}).get("expectancyUSDT") or 0)) if k!="forward" else None,
      } for k,v in validators.items()
  },
  "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
  "note":"CORE_TRIGGER_ONLY historical validation can unlock forward-paper collection only. It never validates FULL_STRATEGY_L2, never enables small-live review, and never enables execution."
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
