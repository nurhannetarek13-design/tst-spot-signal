#!/usr/bin/env python3
"""Gate A x L2 ablation V1.

Research-only diagnostic. Replays the frozen Gate A spot baseline on fixed historical
symbol-days, then applies the already-frozen Gate H persistent L2 confirmation as an
entry veto. It does not alter Gate A, train/tune thresholds, size positions, or enable
live trading.
"""
from __future__ import annotations
import argparse, io, json, pathlib, urllib.parse, urllib.request, zipfile
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd

BINANCE='https://data-api.binance.vision'
BYBIT='https://quote-saver.bycsi.com/orderbook/linear'
COST=.0028; TP=.012; SL=.007; H=16
PERSIST=5; OBI_MIN=.45; MICRO_MIN_BPS=.05


def get_json(url, timeout=45):
    req=urllib.request.Request(url,headers={'User-Agent':'tst-gate-a-l2-ablation/1.0'})
    with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)


def load_klines(sym,start_ms,end_ms):
    cur=start_ms; rows=[]
    while cur<end_ms:
        q=urllib.parse.urlencode({'symbol':sym,'interval':'15m','limit':1000,'startTime':cur,'endTime':end_ms})
        b=get_json(BINANCE+'/api/v3/klines?'+q)
        if not b:break
        rows+=b; nxt=int(b[-1][0])+900000
        if nxt<=cur:break
        cur=nxt
    c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore']
    d=pd.DataFrame(rows,columns=c).drop_duplicates('ot')
    for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
    d['ts']=pd.to_datetime(pd.to_numeric(d.ot),unit='ms',utc=True)
    return d.set_index('ts').sort_index()


def gate_a_features(d,btc):
    f=pd.DataFrame(index=d.index); flow=d.tq/d.qv.replace(0,np.nan)
    r1=d.close.pct_change(1); r4=d.close.pct_change(4)
    volz=(d.qv-d.qv.rolling(96).mean())/d.qv.rolling(96).std()
    flowz=(flow-flow.rolling(96).mean())/flow.rolling(96).std()
    rv=r1.rolling(16).std()*np.sqrt(16); br4=btc.close.pct_change(4).reindex(d.index); rel=r4-br4
    z=.7*flowz+.45*volz+8*r4+4*rel+3*br4-2.5*rv
    p=1/(1+np.exp(-z)); edge=(p-.5)*.03-COST
    return pd.DataFrame({'p':p,'expectedEdge':edge,'close':d.close,'high':d.high,'low':d.low},index=d.index)


def replay_day(sym,f,day):
    start=pd.Timestamp(day,tz='UTC'); end=start+pd.Timedelta(days=1); rows=[]
    i=max(96,int(f.index.searchsorted(start)))
    stop=min(len(f)-H,int(f.index.searchsorted(end)))
    while i<stop:
        r=f.iloc[i]
        if not (np.isfinite(r.p) and r.p>=.65 and r.expectedEdge>0): i+=1; continue
        e=float(r.close); reason='TIMEOUT'; px=float(f.close.iloc[i+H]); exit_i=i+H; mfe=-1e9; mae=1e9
        for j in range(i+1,i+H+1):
            hi=float(f.high.iloc[j]); lo=float(f.low.iloc[j]); mfe=max(mfe,hi/e-1); mae=min(mae,lo/e-1)
            if lo<=e*(1-SL): reason='SL'; px=e*(1-SL); exit_i=j; break
            if hi>=e*(1+TP): reason='TP'; px=e*(1+TP); exit_i=j; break
        rows.append({'symbol':sym,'entryTs':f.index[i].isoformat(),'exitTs':f.index[exit_i].isoformat(),
                     'p':float(r.p),'expectedEdgePct':float(r.expectedEdge*100),'reason':reason,
                     'netPct':float((px/e-1-COST)*100),'mfePct':float(mfe*100),'maePct':float(mae*100)})
        i=exit_i+1
    return rows


def topn(book,reverse,n=10):
    return sorted(((p,q) for p,q in book.items() if q>0),key=lambda x:x[0],reverse=reverse)[:n]


def load_l2_at_entries(symbol,day,entry_ms):
    if not entry_ms:return {}
    targets=sorted(entry_ms); lo=min(targets)-10000; hi=max(targets)+2000
    url=f'{BYBIT}/{symbol}/{day}_{symbol}_ob500.data.zip'
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=240) as r:blob=r.read()
    z=zipfile.ZipFile(io.BytesIO(blob)); name=z.namelist()[0]
    bids={}; asks={}; samples=[]; next_sample=None
    with z.open(name) as fh:
        for raw in fh:
            try:o=json.loads(raw); ts=int(o.get('ts') or 0); d=o['data']
            except Exception:continue
            if o.get('type')=='snapshot':
                bids={float(p):float(q) for p,q in d.get('b',[])}; asks={float(p):float(q) for p,q in d.get('a',[])}
            else:
                for p,q in d.get('b',[]):
                    p=float(p);q=float(q); bids.pop(p,None) if q==0 else bids.__setitem__(p,q)
                for p,q in d.get('a',[]):
                    p=float(p);q=float(q); asks.pop(p,None) if q==0 else asks.__setitem__(p,q)
            if ts<lo:continue
            if ts>hi:break
            if not bids or not asks:continue
            if next_sample is None:next_sample=((ts//1000)+1)*1000
            if ts<next_sample:continue
            b=topn(bids,True,10); a=topn(asks,False,10)
            if not b or not a:continue
            bp,bq=b[0]; ap,aq=a[0]; mid=(bp+ap)/2; bs=sum(q for _,q in b); ass=sum(q for _,q in a); den=bs+ass
            obi=(bs-ass)/den if den else 0.; micro=(ap*bq+bp*aq)/(bq+aq) if (bq+aq)>0 else mid
            samples.append((ts,obi,(micro-mid)/mid*1e4)); next_sample=((ts//1000)+1)*1000
    out={}
    for t in targets:
        w=[x for x in samples if t-5000<=x[0]<t]
        support=len(w)>=PERSIST and all(x[1]>=OBI_MIN for x in w[-PERSIST:]) and np.mean([x[2] for x in w[-PERSIST:]])>=MICRO_MIN_BPS
        last=w[-PERSIST:] if len(w)>=PERSIST else w
        out[t]={'availableSeconds':len(w),'supportive':bool(support),
                'meanObi10':float(np.mean([x[1] for x in last])) if last else None,
                'meanMicropriceBps':float(np.mean([x[2] for x in last])) if last else None}
    return out


def metrics(rows):
    v=np.array([r['netPct'] for r in rows],float)
    if not len(v):return {'n':0,'profitFactor':0,'hitRate':0,'meanNetPct':0,'medianNetPct':0,'totalNetPct':0,'maxDrawdownPct':0}
    wins=v[v>0].sum(); losses=-v[v<0].sum(); eq=np.cumsum(v); peak=np.maximum.accumulate(np.r_[0,eq])[1:]
    return {'n':int(len(v)),'profitFactor':float(wins/losses) if losses>0 else (99. if wins>0 else 0.),
            'hitRate':float((v>0).mean()),'meanNetPct':float(v.mean()),'medianNetPct':float(np.median(v)),
            'totalNetPct':float(v.sum()),'maxDrawdownPct':float((peak-eq).max() if len(v) else 0.)}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--symbol',required=True); ap.add_argument('--date',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    day=datetime.fromisoformat(a.date).replace(tzinfo=timezone.utc); start=day-timedelta(days=5); end=day+timedelta(days=2)
    sm=int(start.timestamp()*1000); em=int(end.timestamp()*1000)
    d=load_klines(a.symbol,sm,em); btc=load_klines('BTCUSDT',sm,em); trades=replay_day(a.symbol,gate_a_features(d,btc),a.date)
    entry_ms=[int(pd.Timestamp(t['entryTs']).timestamp()*1000) for t in trades]
    l2=load_l2_at_entries(a.symbol,a.date,entry_ms)
    kept=[]; vetoed=[]
    for t,ms in zip(trades,entry_ms):
        x=l2.get(ms,{'availableSeconds':0,'supportive':False,'meanObi10':None,'meanMicropriceBps':None}); t['l2']=x
        (kept if x['supportive'] else vetoed).append(t)
    base=metrics(trades); filt=metrics(kept); vet=metrics(vetoed)
    sufficient=filt['n']>=5 and base['n']>=8
    improves=sufficient and filt['profitFactor']>base['profitFactor'] and filt['meanNetPct']>base['meanNetPct'] and filt['maxDrawdownPct']<=base['maxDrawdownPct']
    out={'engine':'GATE_A_X_L2_ABLATION_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'thresholdsFrozen':True,
         'countsTowardForwardGateA':False,'symbol':a.symbol,'date':a.date,
         'gateA':{'pMin':.65,'expectedEdgePositive':True,'tpPct':1.2,'slPct':.7,'timeoutMin':240,'roundTripCostPct':.28},
         'l2EntryVeto':{'persistenceSeconds':PERSIST,'obi10Min':OBI_MIN,'meanMicropriceBpsMin':MICRO_MIN_BPS,'source':'Bybit linear ob500'},
         'baseline':base,'l2Confirmed':filt,'l2Vetoed':vet,'supportRate':float(len(kept)/len(trades)) if trades else None,
         'informationSufficient':bool(sufficient),'improvesBaseline':bool(improves),'trades':trades}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2))
    print(json.dumps({k:out[k] for k in ['symbol','date','baseline','l2Confirmed','l2Vetoed','supportRate','informationSufficient','improvesBaseline']}))

if __name__=='__main__':main()
