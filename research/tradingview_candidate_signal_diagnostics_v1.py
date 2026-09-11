#!/usr/bin/env python3
"""Signal-only diagnostics for TradingView-inspired candidates.
Research only: no position sizing, no TP/SL optimization, no live approval.
Measures raw forward returns + MFE/MAE after each completed-bar signal.
"""
from __future__ import annotations
import argparse, json, pathlib
import numpy as np
import pandas as pd
from tradingview_candidate_discovery_v1 import load, enrich, signals

HORIZONS=(4,8,16,32)  # 1h,2h,4h,8h on 15m bars

def event_stats(d,sig,h):
    idx=np.flatnonzero(sig.fillna(False).to_numpy())
    rows=[]
    for i in idx:
        if i+h>=len(d): continue
        entry=float(d.close.iloc[i])
        if entry<=0: continue
        future=d.iloc[i+1:i+h+1]
        ret=float(d.close.iloc[i+h]/entry-1.0)
        mfe=float(future.high.max()/entry-1.0)
        mae=float(future.low.min()/entry-1.0)
        rows.append((ret,mfe,mae))
    if not rows:
        return {"events":0,"meanReturnPct":0.0,"medianReturnPct":0.0,"hitRate":0.0,"medianMfePct":0.0,"medianMaePct":0.0,"mfeMaeRatio":None}
    a=np.asarray(rows,float); ret=a[:,0]; mfe=a[:,1]; mae=a[:,2]
    med_mfe=float(np.median(mfe)); med_mae=float(np.median(np.abs(mae)))
    return {"events":int(len(ret)),"meanReturnPct":float(ret.mean()*100),"medianReturnPct":float(np.median(ret)*100),"hitRate":float((ret>0).mean()),"medianMfePct":med_mfe*100,"medianMaePct":float(np.median(mae)*100),"mfeMaeRatio":float(med_mfe/med_mae) if med_mae>0 else None}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--start',required=True); ap.add_argument('--end',required=True); ap.add_argument('--symbols',nargs='+',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    start=pd.Timestamp(a.start,tz='UTC'); end=pd.Timestamp(a.end,tz='UTC')
    frames={s:enrich(load(s,start,end)) for s in a.symbols}; result={}
    for name in signals(next(iter(frames.values()))):
        rec={}
        for phase in ('IS','OOS'):
            by={}; pooled={h:[] for h in HORIZONS}
            for sym,d in frames.items():
                k=int(len(d)*0.70); part=d.iloc[:k].copy().reset_index(drop=True) if phase=='IS' else d.iloc[k:].copy().reset_index(drop=True)
                sg=signals(part)[name]
                by[sym]={str(h):event_stats(part,sg,h) for h in HORIZONS}
                idx=np.flatnonzero(sg.fillna(False).to_numpy())
                for h in HORIZONS:
                    for i in idx:
                        if i+h<len(part): pooled[h].append(float(part.close.iloc[i+h]/part.close.iloc[i]-1.0))
            agg={}
            for h,vals in pooled.items():
                x=np.asarray(vals,float)
                agg[str(h)]={"events":int(len(x)),"meanReturnPct":float(x.mean()*100) if len(x) else 0.0,"medianReturnPct":float(np.median(x)*100) if len(x) else 0.0,"hitRate":float((x>0).mean()) if len(x) else 0.0}
            rec[phase]={"aggregate":agg,"bySymbol":by}
        # Diagnostic gate only: positive mean+median and >50% hit on at least 2 horizons OOS, plus no symbol with all horizons negative mean.
        o=rec['OOS']; good_h=sum(1 for h in HORIZONS if o['aggregate'][str(h)]['meanReturnPct']>0 and o['aggregate'][str(h)]['medianReturnPct']>0 and o['aggregate'][str(h)]['hitRate']>0.50)
        symbol_ok=0
        for sym in a.symbols:
            if any(o['bySymbol'][sym][str(h)]['meanReturnPct']>0 for h in HORIZONS): symbol_ok+=1
        rec['diagnosticGate']={"positiveAggregateHorizonsGte2":good_h>=2,"symbolsWithAnyPositiveMeanGte2":symbol_ok>=2,"pass":good_h>=2 and symbol_ok>=2}
        result[name]=rec
    out={"authorization":"RESEARCH_ONLY","liveTrading":False,"decision":"SIGNAL_DIAGNOSTIC_ONLY","timeframe":"15m","entryReference":"signal close","horizonsBars":list(HORIZONS),"period":{"start":a.start,"end":a.end,"split":"70pct_IS_30pct_OOS"},"symbols":a.symbols,"candidates":result}
    p=pathlib.Path(a.out); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,indent=2),encoding='utf-8'); print(json.dumps(out,indent=2))
if __name__=='__main__': main()
