"""Research integrity: drift, multiple-testing control, baselines and strategy dependency."""
from __future__ import annotations
import math

def psi(expected, actual, eps=1e-9):
    if len(expected)!=len(actual) or not expected:return None
    total=0.0
    for e,a in zip(expected,actual):
        e=max(eps,float(e)); a=max(eps,float(a)); total+=(a-e)*math.log(a/e)
    return total

def drift_report(reference, current, *, warn=.10, block=.25):
    out={}; blocked=False
    for name,base in (reference or {}).items():
        cur=(current or {}).get(name)
        if not isinstance(base,list) or not isinstance(cur,list): out[name]={"state":"UNKNOWN"}; blocked=True; continue
        v=psi(base,cur)
        state="BLOCK" if v is None or v>=block else "WARN" if v>=warn else "STABLE"
        blocked|=state=="BLOCK"; out[name]={"psi":v,"state":state}
    return {"features":out,"block_promotion":blocked,"diagnostic_only":True}

def false_discovery_guard(results, *, alpha=.05):
    """Benjamini-Hochberg FDR; records full hypothesis count."""
    rows=sorted([dict(r) for r in results or []],key=lambda x:float(x.get("p_value",1)))
    m=len(rows); accepted=[]
    for i,r in enumerate(rows,1):
        r["bh_threshold"]=alpha*i/m if m else 0
        r["fdr_candidate"]=float(r.get("p_value",1))<=r["bh_threshold"]
    k=max([i for i,r in enumerate(rows,1) if r["fdr_candidate"]] or [0])
    for i,r in enumerate(rows,1): r["fdr_accepted"]=i<=k
    return {"hypotheses_tested":m,"accepted":sum(r["fdr_accepted"] for r in rows),"rows":rows,"diagnostic_only":True}

def baseline_comparison(strategy_net, baselines):
    s=sum(map(float,strategy_net or []))
    out={}
    for name,vals in (baselines or {}).items():
        b=sum(map(float,vals or [])); out[name]={"strategy_net":s,"baseline_net":b,"excess_net":s-b,"beats_baseline":s>b}
    return {"baselines":out,"complexity_justified":bool(out) and all(x["beats_baseline"] for x in out.values()),"diagnostic_only":True}

def strategy_dependency(signal_returns, *, threshold=.7):
    names=sorted(signal_returns); edges=[]
    def corr(a,b):
        n=min(len(a),len(b))
        if n<5:return None
        a=list(map(float,a[-n:])); b=list(map(float,b[-n:])); ma=sum(a)/n; mb=sum(b)/n
        x=sum((v-ma)**2 for v in a); y=sum((v-mb)**2 for v in b)
        return None if x<=0 or y<=0 else sum((a[i]-ma)*(b[i]-mb) for i in range(n))/math.sqrt(x*y)
    for i,a in enumerate(names):
        for b in names[i+1:]:
            c=corr(signal_returns[a],signal_returns[b])
            if c is not None and abs(c)>=threshold: edges.append({"a":a,"b":b,"correlation":c})
    return {"dependency_edges":edges,"nominal_strategies":len(names),"independence_review_required":bool(edges)}
