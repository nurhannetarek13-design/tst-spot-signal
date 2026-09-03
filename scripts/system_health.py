#!/usr/bin/env python3
import datetime as dt, json, pathlib

ROOT=pathlib.Path(".")
OUT=pathlib.Path("validation/system-health-latest.json")

def load(p):
    p=pathlib.Path(p)
    return json.loads(p.read_text()) if p.exists() else None

checks=[]
def check(name, ok, detail=None):
    checks.append({"name":name,"ok":bool(ok),"detail":detail})
    return bool(ok)

policy=load("fusion/policy.json")
manifest=load("validation/fusion/candidate-manifest.json")
gate=load("validation/fusion/gate-latest.json")
deriv=load("validation/edges/derivatives-pressure-latest.json")
forward=load("validation/fusion/forward-latest.json")
worker=pathlib.Path("src/edge-worker.js").read_text() if pathlib.Path("src/edge-worker.js").exists() else ""

check("policy_exists",policy is not None)
check("policy_paper_only",policy and policy.get("mode")=="PAPER_ONLY")
check("policy_live_off",policy and policy.get("liveTrading") is False)
check("executor_auto_enable_off",policy and (policy.get("execution") or {}).get("autoEnableLive") is False)
check("withdrawals_off",policy and (policy.get("execution") or {}).get("withdrawalsAllowed") is False)
check("futures_off",policy and (policy.get("execution") or {}).get("futuresAllowed") is False)
check("leverage_off",policy and (policy.get("execution") or {}).get("leverageAllowed") is False)

check("manifest_exists",manifest is not None)
check("manifest_live_off",manifest and manifest.get("liveTrading") is False)
check("gate_exists",gate is not None)
check("gate_live_off",gate and gate.get("liveTrading") is False)
check("gate_auto_enable_off",gate and gate.get("executorAutoEnable") is False)

fp=(manifest or {}).get("candidateFingerprint")
if fp:
    check("gate_candidate_matches",gate and gate.get("candidateFingerprint")==fp,{"manifest":fp,"gate":(gate or {}).get("candidateFingerprint")})
else:
    check("no_candidate_is_fail_closed",gate and gate.get("liveReady") is False and "CANDIDATE:MISSING" in (gate.get("reasons") or []),{"manifestStatus":(manifest or {}).get("status")})

check("forward_live_off",forward and forward.get("liveTrading") is False)
check("derivatives_context_read_only",deriv and deriv.get("liveTrading") is False)
check("derivatives_has_data",deriv and int(deriv.get("usableCount") or 0)>=1,{"usableCount":(deriv or {}).get("usableCount")})

check("worker_live_off_literal","liveTrading:false" in worker)
check("worker_unified_gate","TST_UNIFIED_CANDIDATE_V1" in worker and "candidateFingerprint" in worker)
check("worker_small_mid_universe","LIQUID_SMALL_MID_CAP_USDT_PLUS_NEW_LISTINGS" in worker)
check("worker_microstructure","MICROSTRUCTURE_COMPOSITE" in worker and "micropriceOffsetBps" in worker)
check("worker_derivatives_context","DERIVATIVES_SNAPSHOT_URL" in worker and "derivativesPressure" in worker)
check("worker_forward_validator","forward-latest.json" in worker)

required=[
 ".github/workflows/public-edge-lab.yml",
 ".github/workflows/candidate-vectorbt-validator.yml",
 ".github/workflows/fusion-freqtrade-validator.yml",
 ".github/workflows/fusion-jesse-validator.yml",
 ".github/workflows/nautilus-validator.yml",
 ".github/workflows/forward-paper-public-edges.yml",
 ".github/workflows/fusion-gate.yml",
 ".github/workflows/candidate-rotation.yml",
 ".github/workflows/derivatives-pressure.yml",
 ".github/workflows/deploy-cloudflare.yml",
]
for p in required:
    check("workflow:"+pathlib.Path(p).name,pathlib.Path(p).exists())

ok=all(x["ok"] for x in checks)
report={
 "engine":"SYSTEM_HEALTH",
 "status":"PASS" if ok else "FAIL",
 "pass":ok,
 "liveTrading":False,
 "candidateId":(manifest or {}).get("candidateId"),
 "candidateFingerprint":fp,
 "candidateStatus":(manifest or {}).get("status") or ("ACTIVE" if fp else None),
 "gateLiveReady":bool((gate or {}).get("liveReady")),
 "checks":checks,
 "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
 "note":"PASS means the research/paper architecture is internally fail-closed and wired. It does not mean a profitable live strategy exists."
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
if not ok:
    raise SystemExit(1)
