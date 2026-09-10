#!/usr/bin/env python3
"""Compare our L2 replay semantics against Binance/pfei-sa canonical rules.
Research-only diagnostic. No trading actions.
"""
from __future__ import annotations
import argparse, hashlib, json, pathlib, urllib.request
from bisect import bisect_left
from decimal import Decimal
import pyarrow.parquet as pq

BASE='https://huggingface.co/datasets/MaximumLeverage/crypto-lob-stream/resolve/main/'
MONTH='2026-07'
MAX_DEPTH=1000


def dl(url,path):
    p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and p.stat().st_size: return p
    req=urllib.request.Request(url,headers={'User-Agent':'tst-l2-diff/1.1'})
    with urllib.request.urlopen(req,timeout=240) as r,p.open('wb') as f:
        while True:
            b=r.read(8*1024*1024)
            if not b: break
            f.write(b)
    return p


def prune(book, side):
    if len(book)<=MAX_DEPTH: return
    keys=sorted(book, reverse=(side=='bid'))[:MAX_DEPTH]
    keep=set(keys)
    for k in list(book):
        if k not in keep: del book[k]


def apply(book, side, price, qty):
    p=Decimal(str(price)); q=Decimal(str(qty))
    if q==0: book.pop(p,None)
    else: book[p]=q


def state_hash(bids,asks):
    parts=[]
    for p in sorted(bids,reverse=True): parts.append(f'b|{p}|{bids[p]}')
    for p in sorted(asks): parts.append(f'a|{p}|{asks[p]}')
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def topn(bids,asks,n=10):
    return {
      'bids': [[str(p),str(bids[p])] for p in sorted(bids,reverse=True)[:n]],
      'asks': [[str(p),str(asks[p])] for p in sorted(asks)[:n]],
    }


def load_snapshots(path):
    t=pq.read_table(path,columns=['timestamp_ms','last_update_id','side','price','quantity']).to_pylist()
    out=[]; cur=None; rows=[]
    for r in sorted(t,key=lambda z:(int(z['timestamp_ms']),int(z['last_update_id']),str(z['side']),float(z['price']))):
        key=(int(r['timestamp_ms']),int(r['last_update_id']))
        if cur is None: cur=key
        if key!=cur:
            out.append((cur[0],cur[1],rows)); cur=key; rows=[]
        rows.append(r)
    if cur is not None: out.append((cur[0],cur[1],rows))
    return out


def load_groups(path):
    pf=pq.ParquetFile(path)
    groups=[]; cur=None; rows=[]
    for batch in pf.iter_batches(batch_size=250000,columns=['timestamp_ms','side','price','quantity','first_update_id','last_update_id']):
        for r in batch.to_pylist():
            key=(int(r['timestamp_ms']),int(r['first_update_id']),int(r['last_update_id']))
            if cur is None: cur=key
            if key!=cur:
                groups.append((cur,rows)); cur=key; rows=[]
            rows.append(r)
    if cur is not None: groups.append((cur,rows))
    # Canonical sequence ordering. Timestamp is diagnostic only.
    groups.sort(key=lambda g:(g[0][2],g[0][1],g[0][0]))
    return groups


def build_from_snapshot(rows):
    bids={}; asks={}
    for r in rows:
        side=str(r['side']); book=bids if side=='bid' else asks
        apply(book,side,r['price'],r['quantity'])
    prune(bids,'bid'); prune(asks,'ask')
    return bids,asks


def replay_reference(snapshot, groups, limit):
    _,sid,srows=snapshot
    bids,asks=build_from_snapshot(srows)
    want=sid+1; started=False; prev_u=None; trace=[]
    for (ts,U,u),rows in groups:
        if u<=sid: continue
        if not started:
            if U<=want<=u: started=True
            elif U>want:
                return trace, {'status':'FIRST_EVENT_GAP','snapshotId':sid,'expected':want,'U':U,'u':u}
            else: continue
        else:
            if U!=prev_u+1:
                return trace, {'status':'SEQUENCE_GAP','prev_u':prev_u,'expected':prev_u+1,'U':U,'u':u,'timestamp_ms':ts}
        for r in rows:
            side=str(r['side']); apply(bids if side=='bid' else asks,side,r['price'],r['quantity'])
        prune(bids,'bid'); prune(asks,'ask')
        prev_u=u
        if bids and asks and max(bids)>=min(asks):
            return trace, {'status':'CROSSED_BOOK','U':U,'u':u,'timestamp_ms':ts,'top':topn(bids,asks,3)}
        trace.append({'timestamp_ms':ts,'U':U,'u':u,'hash':state_hash(bids,asks),'top10':topn(bids,asks,10)})
        if len(trace)>=limit: break
    return trace, {'status':'OK','events':len(trace),'last_u':prev_u}


def replay_candidate(snapshot, groups, limit):
    _,sid,srows=snapshot
    bids,asks=build_from_snapshot(srows); anchor=sid; trace=[]; prev_u=None; started=False
    for (ts,U,u),rows in groups:
        if u<=anchor: continue
        if not started:
            if not (U<=anchor+1<=u):
                if U>anchor+1: return trace, {'status':'FIRST_EVENT_GAP','expected':anchor+1,'U':U,'u':u}
                continue
            started=True
        elif U!=prev_u+1:
            return trace, {'status':'SEQUENCE_GAP','prev_u':prev_u,'expected':prev_u+1,'U':U,'u':u,'timestamp_ms':ts}
        for r in rows:
            side=str(r['side']); apply(bids if side=='bid' else asks,side,r['price'],r['quantity'])
        prune(bids,'bid'); prune(asks,'ask')
        prev_u=u; anchor=u
        if bids and asks and max(bids)>=min(asks):
            return trace, {'status':'CROSSED_BOOK','U':U,'u':u,'timestamp_ms':ts,'top':topn(bids,asks,3)}
        trace.append({'timestamp_ms':ts,'U':U,'u':u,'hash':state_hash(bids,asks),'top10':topn(bids,asks,10)})
        if len(trace)>=limit: break
    return trace, {'status':'OK','events':len(trace),'last_u':prev_u}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--symbol',default='SOLUSDT',choices=['BTCUSDT','ETHUSDT','SOLUSDT']); ap.add_argument('--limit',type=int,default=1000); a=ap.parse_args()
    root=pathlib.Path('/tmp/l2diff')
    dp=dl(BASE+f'depth/binance/{a.symbol}/{MONTH}.parquet',root/f'{a.symbol}-depth.parquet')
    sp=dl(BASE+f'snapshots/binance/{a.symbol}/{MONTH}.parquet',root/f'{a.symbol}-snap.parquet')
    snaps=load_snapshots(sp); groups=load_groups(dp)
    uvals=[g[0][2] for g in groups]
    results=[]
    for si,snap in enumerate(snaps):
        # Binance alignment is ID-based: find first diff whose u can cover lastUpdateId+1.
        start=max(0,bisect_left(uvals,snap[1]+1)-1)
        subset=groups[start:]
        ref,rs=replay_reference(snap,subset,a.limit); cand,cs=replay_candidate(snap,subset,a.limit)
        div=None
        for i,(x,y) in enumerate(zip(ref,cand)):
            if x['hash']!=y['hash'] or x['U']!=y['U'] or x['u']!=y['u']:
                div={'index':i,'reference':x,'candidate':y}; break
        if div is None and len(ref)!=len(cand):
            div={'index':min(len(ref),len(cand)),'referenceLength':len(ref),'candidateLength':len(cand)}
        results.append({'snapshotIndex':si,'snapshotTs':snap[0],'snapshotId':snap[1],'referenceStatus':rs,'candidateStatus':cs,'compared':min(len(ref),len(cand)),'firstDivergence':div})
    out={'symbol':a.symbol,'month':MONTH,'snapshots':len(snaps),'groups':len(groups),'limitPerSnapshot':a.limit,'results':results}
    bad=[r for r in results if r['firstDivergence']]
    valid=[r for r in results if r['compared']>0 and r['referenceStatus']['status'] in ('OK','SEQUENCE_GAP')]
    out['divergenceCount']=len(bad); out['validSnapshotCount']=len(valid); out['comparedEvents']=sum(r['compared'] for r in results)
    out['firstDivergence']=bad[0] if bad else None
    print(json.dumps(out,indent=2))

if __name__=='__main__': main()
