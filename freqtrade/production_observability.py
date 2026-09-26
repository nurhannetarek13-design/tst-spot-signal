"""Operational metrics, feature flags/canary assignment, and post-mortems."""
from __future__ import annotations
from collections import defaultdict
from hashlib import sha256
import json,time

class Metrics:
    def __init__(self): self.counts=defaultdict(int); self.samples=defaultdict(list)
    def inc(self,name,n=1): self.counts[str(name)]+=int(n)
    def observe(self,name,value,max_samples=1000):
        a=self.samples[str(name)]; a.append(float(value)); del a[:-int(max_samples)]
    def snapshot(self):
        out={"counts":dict(self.counts),"samples":{}}
        for k,v in self.samples.items():
            s=sorted(v); n=len(s)
            out["samples"][k]={"n":n,"avg":sum(s)/n if n else None,
              "p95":s[min(n-1,int((n-1)*.95))] if n else None,"max":max(s) if n else None}
        return out

def feature_enabled(name,signal_id,flags):
    cfg=(flags or {}).get(name) or {}
    if cfg.get("enabled") is not True:return False
    pct=max(0,min(100,int(cfg.get("percent",100))))
    bucket=int(sha256(f"{name}:{signal_id}".encode()).hexdigest()[:8],16)%100
    return bucket<pct

def build_postmortem(event:dict,context:dict,spec:dict|None=None):
    expected=(spec or {}).get("expected_behavior")
    actual=event.get("behavior")
    return {"generated_at":time.time(),"event_type":event.get("type"),"severity":event.get("severity","UNKNOWN"),
      "what_happened":event.get("summary") or event.get("type"),
      "data_available":context,
      "decision":event.get("decision"),
      "expected_behavior":expected,
      "behavior_matched_spec":None if expected is None or actual is None else actual==expected,
      "version":context.get("version"),"diagnostic_only":True}
