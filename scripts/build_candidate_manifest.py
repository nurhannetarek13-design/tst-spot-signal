#!/usr/bin/env python3
import datetime as dt
import hashlib
import json
import pathlib

EDGE=pathlib.Path("validation/edges/public-edge-lab-latest.json")
VBT=pathlib.Path("validation/fusion/vectorbt-latest.json")
REJECTED=pathlib.Path("validation/fusion/rejected-candidates.json")
FROZEN_FIXTURE=pathlib.Path("validation/fusion/frozen-parity-candidate.json")
OUT=pathlib.Path("validation/fusion/candidate-manifest.json")
POOL_SIZE=8
REJECT_TTL_DAYS=7
MIN_STRESSED_TRADES=100
MIN_BASE_PF=1.15
MIN_STRESS_PF=1.00
MAX_DD_USDT=4.0

COMPATIBLE_EDGE={
  "TS_MOMENTUM":{"timeframe":"1h","params":{"emaFast":48,"emaSlow":120,"retLookback":24,"retMin":0.02,"atrMin":0.006,"atrMax":0.08,"relvol":0.8,"holdBars":24,"sl":0.03,"tp":0.06}},
  "LIQUIDITY_REVERSAL":{"timeframe":"1h","params":{"retLookback":6,"zLookback":720,"zMax":-2.0,"rsiMax":35,"volumeRatioMax":1.10,"holdBars":12,"sl":0.03,"tp":0.05}},
  "VOLATILITY_BREAKOUT":{"timeframe":"1h","params":{"lookback":24,"compressionLookback":72,"compressionPct":0.25,"relvol":1.5,"rsiMin":55,"rsiMax":75,"holdBars":12,"sl":0.025,"tp":0.055}},
  "CROSS_CRYPTO_LEAD_LAG":{"timeframe":"1h","params":{"leaderRetMin":0.012,"gapMin":0.008,"altRetMin":-0.02,"relvol":0.9,"rsiMin":42,"rsiMax":70,"emaFast":24,"holdBars":6,"sl":0.025,"tp":0.05}},
}
COMPATIBLE_VBT={"TREND_BREAKOUT","MEAN_REVERSION","VOLATILITY_MOMENTUM"}

def load(p,default):
    try:return json.loads(p.read_text())
    except Exception:return default

def fingerprint(x):
    raw=json.dumps(x,sort_keys=True,separators=(",",":")).encode()
    return hashlib.sha256(raw).hexdigest()[:20]

def enrich(c):
    core={k:c[k] for k in ["source","symbol","family","timeframe","params"]}
    c=dict(c)
    c["candidateId"]=f"{c['source']}:{c['symbol']}:{c['family']}"
    c["candidateFingerprint"]=fingerprint(core)
    c["eligibleForPromotion"]=True
    c["admissionGateVersion"]="STRICT_SAMPLE_V2"
    return c

def recent_rejections():
    rows=load(REJECTED,{"rejected":[]}).get("rejected",[])
    now=dt.datetime.now(dt.timezone.utc)
    out=set()
    for r in rows:
        try:
            ts=dt.datetime.fromisoformat(str(r["rejectedAt"]).replace("Z","+00:00"))
            if (now-ts).total_seconds() <= REJECT_TTL_DAYS*86400:
                fp=r.get("candidateFingerprint")
                if fp: out.add(fp)
        except Exception:
            pass
    return out

def permanent_exclusions():
    f=load(FROZEN_FIXTURE,{})
    if f.get("fixtureRole")=="GOLDEN_CANONICAL_REGRESSION_ONLY" and f.get("candidateFingerprint"):
        return {f["candidateFingerprint"]}
    return set()

def economic_gate(base,stress):
    try:
        n=int(stress.get("trades",0) or 0)
        bpf=float(base.get("profitFactor",0) or 0)
        spf=float(stress.get("profitFactor",0) or 0)
        be=float(base.get("expectancyUSDT",0) or 0)
        se=float(stress.get("expectancyUSDT",0) or 0)
        dd=base.get("maxDrawdownUSDT")
        dd_ok=True if dd is None else float(dd)<=MAX_DD_USDT
        return n>=MIN_STRESSED_TRADES and bpf>=MIN_BASE_PF and spf>=MIN_STRESS_PF and be>0 and se>0 and dd_ok
    except Exception:
        return False

rejected=recent_rejections()
permanent=permanent_exclusions()
blocked=rejected|permanent
candidates=[]

if EDGE.exists():
    e=load(EDGE,{})
    meta={x["symbol"]:x for x in e.get("universe",[]) if x.get("symbol")}
    for x in e.get("topSymbolCandidates",[]):
        fam=x.get("family"); sym=x.get("symbol")
        if fam not in COMPATIBLE_EDGE or not sym: continue
        m=meta.get(sym,{})
        qv=float(m.get("quoteVolume24h") or 0); price=float(m.get("price") or 0)
        stress=x.get("stress2x") or {}; base=x.get("base") or {}
        if not (20_000_000<=qv<=150_000_000 and 0<price<=3 and economic_gate(base,stress)):
            continue
        spec=COMPATIBLE_EDGE[fam]
        c=enrich({
          "source":"PUBLIC_EDGE_LAB","sourceGeneratedAt":e.get("generatedAt"),"symbol":sym,"family":fam,
          "timeframe":spec["timeframe"],"params":spec["params"],"sourceMetrics":{"base":base,"stress2x":stress},
          "liquidity":{"quoteVolume24h":qv,"priceUSDT":price},
          "rankKey":[1,float(stress.get("expectancyUSDT",0)),float(stress.get("profitFactor",0)),int(stress.get("trades",0))]
        })
        if c["candidateFingerprint"] not in blocked:
            candidates.append(c)

if VBT.exists():
    v=load(VBT,{})
    rows=[v.get("selected"),v.get("selectedPaperCandidate")]+list(v.get("finalists") or [])
    seen=set()
    for x in rows:
        if not x: continue
        key=x.get("candidateId")
        if not key or key in seen: continue
        seen.add(key)
        fam=x.get("family"); sym=x.get("symbol")
        if fam not in COMPATIBLE_VBT or not sym: continue
        qv=float(x.get("quoteVolume24h") or 0)
        stress=x.get("holdoutStress2x") or {}; base=x.get("holdoutBase") or {}
        if not (20_000_000<=qv<=150_000_000 and economic_gate(base,stress)):
            continue
        c=enrich({
          "source":"VECTORBT","sourceGeneratedAt":v.get("generatedAt"),"symbol":sym,"family":fam,"timeframe":"15m",
          "params":x.get("params") or {},"sourceMetrics":{"base":base,"stress2x":stress},
          "liquidity":{"quoteVolume24h":qv,"priceUSDT":None},
          "rankKey":[0,float(stress.get("expectancyUSDT",0)),float(stress.get("profitFactor",0)),int(stress.get("trades",0))]
        })
        if c["candidateFingerprint"] not in blocked:
            candidates.append(c)

# Remove duplicate fingerprints while preserving best rank.
dedup={}
for c in candidates:
    fp=c["candidateFingerprint"]
    if fp not in dedup or tuple(c["rankKey"])>tuple(dedup[fp]["rankKey"]):
        dedup[fp]=c
candidates=sorted(dedup.values(),key=lambda x:tuple(x["rankKey"]),reverse=True)[:POOL_SIZE]

selected=candidates[0] if candidates else None
now=dt.datetime.now(dt.timezone.utc).isoformat()
admission={
  "version":"STRICT_SAMPLE_V2",
  "minStressedTrades":MIN_STRESSED_TRADES,
  "minBaseProfitFactor":MIN_BASE_PF,
  "minStressProfitFactor":MIN_STRESS_PF,
  "requirePositiveBaseExpectancy":True,
  "requirePositiveStressExpectancy":True,
  "maxBaseDrawdownUSDTWhenAvailable":MAX_DD_USDT,
  "blockedFingerprints":sorted(blocked),
}

if selected:
    manifest={
      "strategyId":"TST_UNIFIED_CANDIDATE_V1",
      **selected,
      "candidatePool":candidates,
      "poolSize":len(candidates),
      "admissionGate":admission,
      "recentRejectedFingerprints":sorted(rejected),
      "permanentExcludedFingerprints":sorted(permanent),
      "authorization":"VALIDATION_AND_FORWARD_PAPER_ONLY",
      "liveTrading":False,
      "validatorsRequired":["vectorbt","freqtrade","jesse","nautilus","forward"],
      "generatedAt":now,
    }
else:
    manifest={
      "strategyId":"TST_UNIFIED_CANDIDATE_V1","candidateId":None,"candidateFingerprint":None,
      "status":"NO_CANDIDATE_PASSES_STRICT_ADMISSION","candidatePool":[],"poolSize":0,
      "admissionGate":admission,
      "recentRejectedFingerprints":sorted(rejected),
      "permanentExcludedFingerprints":sorted(permanent),
      "authorization":"RESEARCH_ONLY","liveTrading":False,
      "validatorsRequired":["vectorbt","freqtrade","jesse","nautilus","forward"],
      "generatedAt":now,
    }

OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(manifest,indent=2))
print(json.dumps(manifest,indent=2))
