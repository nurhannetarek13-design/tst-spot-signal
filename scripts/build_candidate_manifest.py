#!/usr/bin/env python3
import datetime as dt
import hashlib
import json
import pathlib

EDGE=pathlib.Path("validation/edges/public-edge-lab-latest.json")
VBT=pathlib.Path("validation/fusion/vectorbt-latest.json")
OUT=pathlib.Path("validation/fusion/candidate-manifest.json")

COMPATIBLE_EDGE={
  "TS_MOMENTUM":{"timeframe":"1h","params":{"emaFast":48,"emaSlow":120,"retLookback":24,"retMin":0.02,"atrMin":0.006,"atrMax":0.08,"relvol":0.8,"holdBars":24,"sl":0.03,"tp":0.06}},
  "LIQUIDITY_REVERSAL":{"timeframe":"1h","params":{"retLookback":6,"zLookback":720,"zMax":-2.0,"rsiMax":35,"volumeRatioMax":1.10,"holdBars":12,"sl":0.03,"tp":0.05}},
  "VOLATILITY_BREAKOUT":{"timeframe":"1h","params":{"lookback":24,"compressionLookback":72,"compressionPct":0.25,"relvol":1.5,"rsiMin":55,"rsiMax":75,"holdBars":12,"sl":0.025,"tp":0.055}},
}
COMPATIBLE_VBT={"TREND_BREAKOUT","MEAN_REVERSION","VOLATILITY_MOMENTUM"}

def fp(x):
    raw=json.dumps(x,sort_keys=True,separators=(",",":")).encode()
    return hashlib.sha256(raw).hexdigest()[:20]

candidates=[]
if EDGE.exists():
    e=json.loads(EDGE.read_text())
    meta={x["symbol"]:x for x in e.get("universe",[])}
    for x in e.get("topSymbolCandidates",[]):
        fam=x.get("family"); sym=x.get("symbol")
        if fam not in COMPATIBLE_EDGE or not x.get("paperCandidate"):
            continue
        m=meta.get(sym,{})
        qv=float(m.get("quoteVolume24h") or 0); price=float(m.get("price") or 0)
        stress=x.get("stress2x") or {}
        if not (20_000_000<=qv<=150_000_000 and 0<price<=3 and stress.get("expectancyUSDT",0)>0 and stress.get("profitFactor",0)>=1.0):
            continue
        spec=COMPATIBLE_EDGE[fam]
        candidates.append({
          "source":"PUBLIC_EDGE_LAB","sourceGeneratedAt":e.get("generatedAt"),"symbol":sym,"family":fam,
          "timeframe":spec["timeframe"],"params":spec["params"],"sourceMetrics":{"base":x.get("base"),"stress2x":stress},
          "liquidity":{"quoteVolume24h":qv,"priceUSDT":price},
          "rankKey":[1,float(stress.get("expectancyUSDT",0)),float(stress.get("profitFactor",0)),int(stress.get("trades",0))]
        })

if VBT.exists():
    v=json.loads(VBT.read_text())
    rows=[v.get("selected"),v.get("selectedPaperCandidate")]+list(v.get("finalists") or [])
    seen=set()
    for x in rows:
        if not x: continue
        key=x.get("candidateId")
        if not key or key in seen: continue
        seen.add(key)
        fam=x.get("family"); sym=x.get("symbol")
        if fam not in COMPATIBLE_VBT: continue
        qv=float(x.get("quoteVolume24h") or 0)
        stress=x.get("holdoutStress2x") or {}
        if not (20_000_000<=qv<=150_000_000 and stress.get("expectancyUSDT",0)>0 and stress.get("profitFactor",0)>=1.0):
            continue
        candidates.append({
          "source":"VECTORBT","sourceGeneratedAt":v.get("generatedAt"),"symbol":sym,"family":fam,"timeframe":"15m",
          "params":x.get("params") or {},"sourceMetrics":{"base":x.get("holdoutBase"),"stress2x":stress},
          "liquidity":{"quoteVolume24h":qv,"priceUSDT":None},
          "rankKey":[0,float(stress.get("expectancyUSDT",0)),float(stress.get("profitFactor",0)),int(stress.get("trades",0))]
        })

candidates.sort(key=lambda x:tuple(x["rankKey"]),reverse=True)
if candidates:
    selected=candidates[0]
    core={k:selected[k] for k in ["source","symbol","family","timeframe","params"]}
    fingerprint=fp(core)
    manifest={
      "strategyId":"TST_UNIFIED_CANDIDATE_V1",
      "candidateId":f"{selected['source']}:{selected['symbol']}:{selected['family']}",
      "candidateFingerprint":fingerprint,
      **selected,
      "authorization":"VALIDATION_AND_FORWARD_PAPER_ONLY",
      "liveTrading":False,
      "validatorsRequired":["vectorbt","freqtrade","jesse","nautilus","forward"],
      "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
else:
    manifest={
      "strategyId":"TST_UNIFIED_CANDIDATE_V1","candidateId":None,"candidateFingerprint":None,
      "status":"NO_COMPATIBLE_CANDIDATE","authorization":"RESEARCH_ONLY","liveTrading":False,
      "validatorsRequired":["vectorbt","freqtrade","jesse","nautilus","forward"],
      "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(manifest,indent=2))
print(json.dumps(manifest,indent=2))
