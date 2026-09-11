#!/usr/bin/env python3
"""Public Bybit L2 snapshot feature. RESEARCH ONLY.
No credentials, no orders, no exchange account access.
"""
from __future__ import annotations
import json, urllib.parse, urllib.request

BASE='https://api.bybit.com'

def compute(bids, asks, depth=10):
    b=sorted(((float(p),float(q)) for p,q in bids if float(q)>0),key=lambda x:x[0],reverse=True)[:depth]
    a=sorted(((float(p),float(q)) for p,q in asks if float(q)>0),key=lambda x:x[0])[:depth]
    if not b or not a: raise ValueError('empty_book')
    bp,bq=b[0]; ap,aq=a[0]
    if ap<=bp: raise ValueError('crossed_book')
    mid=(bp+ap)/2.0
    bsum=sum(q for _,q in b); asum=sum(q for _,q in a); den=bsum+asum
    obi=(bsum-asum)/den if den else 0.0
    micro=(ap*bq+bp*aq)/(bq+aq) if (bq+aq)>0 else mid
    micro_bps=(micro-mid)/mid*1e4
    spread_bps=(ap-bp)/mid*1e4
    return {'best_bid':bp,'best_ask':ap,'mid':mid,'obi10':obi,'microprice':micro,'microprice_bps':micro_bps,'spread_bps':spread_bps,
            'long_confirm':bool(obi>=0.60 and micro_bps>=0.08),
            'short_confirm':bool(obi<=-0.60 and micro_bps<=-0.08)}

def snapshot(symbol:str, limit:int=50):
    q=urllib.parse.urlencode({'category':'linear','symbol':symbol,'limit':limit})
    req=urllib.request.Request(BASE+'/v5/market/orderbook?'+q,headers={'User-Agent':'tst-shadow-l2/1.0'})
    with urllib.request.urlopen(req,timeout=15) as r: payload=json.load(r)
    if int(payload.get('retCode',-1))!=0: raise RuntimeError(payload)
    x=payload['result']; out=compute(x.get('b',[]),x.get('a',[]),10)
    out.update({'symbol':symbol,'exchange':'BYBIT','category':'linear','server_ts_ms':int(payload.get('time') or 0),'update_id':x.get('u')})
    return out

def selftest():
    # Strong long imbalance and microprice above mid.
    b=[['100','8'],['99.9','5'],['99.8','4']]; a=[['100.1','1'],['100.2','1'],['100.3','1']]
    x=compute(b,a); assert x['obi10']>0.60 and x['microprice_bps']>0.08 and x['long_confirm'] is True
    # Balanced book cannot confirm.
    y=compute([['100','2']],[['100.1','2']]); assert y['long_confirm'] is False and y['short_confirm'] is False
    print(json.dumps({'selftest':'PASS','long_fixture':x,'balanced_fixture':y}))

if __name__=='__main__':
    selftest()
