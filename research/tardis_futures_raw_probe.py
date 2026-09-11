#!/usr/bin/env python3
"""Probe Tardis raw Binance USD-M Futures feed for canonical U/u/pu replay fields.
Research-only diagnostic. No trading actions.
"""
from __future__ import annotations
import argparse, json, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE='https://api.tardis.dev/v1/data-feeds/binance-futures'

def fetch_slice(symbol, start, end):
    filters=json.dumps([
        {'channel':'depth','symbols':[symbol]},
        {'channel':'depthSnapshot','symbols':[symbol]},
    ], separators=(',',':'))
    qs=urllib.parse.urlencode({'from':start,'to':end,'filters':filters})
    req=urllib.request.Request(BASE+'?'+qs,headers={'User-Agent':'tst-tardis-probe/1.0'})
    with urllib.request.urlopen(req,timeout=180) as r:
        body=r.read().decode('utf-8','replace')
        headers=dict(r.headers)
    return body, headers

def parse_lines(body):
    out=[]
    for line in body.splitlines():
        line=line.strip()
        if not line: continue
        # Tardis raw feed lines are commonly: local_timestamp + JSON payload.
        if ' ' in line and not line.startswith('{'):
            ts, payload=line.split(' ',1)
        else:
            ts, payload=None, line
        try:
            obj=json.loads(payload)
        except Exception:
            try: obj=json.loads(line); ts=None
            except Exception: continue
        out.append((ts,obj))
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--symbol',default='BTCUSDT')
    ap.add_argument('--from-ts',default='2024-01-01T00:00:00')
    ap.add_argument('--to-ts',default='2024-01-01T00:01:00')
    a=ap.parse_args()
    body,headers=fetch_slice(a.symbol,a.from_ts,a.to_ts)
    msgs=parse_lines(body)
    depth=[]; snaps=[]
    for ts,o in msgs:
        if not isinstance(o,dict): continue
        e=o.get('e') or o.get('event_type')
        if e=='depthUpdate' or {'U','u'}.issubset(o): depth.append((ts,o))
        if 'lastUpdateId' in o and ('bids' in o or 'asks' in o): snaps.append((ts,o))
    pu_count=sum(1 for _,o in depth if 'pu' in o)
    continuity=[]
    prev=None
    for ts,o in depth[:5000]:
        if prev is not None and 'pu' in o:
            continuity.append(int(o['pu'])==int(prev['u']))
        prev=o
    sample_depth=depth[0][1] if depth else None
    sample_snap=snaps[0][1] if snaps else None
    result={
      'authorization':'RESEARCH_ONLY','symbol':a.symbol,'from':a.from_ts,'to':a.to_ts,
      'httpHeaders':{k:v for k,v in headers.items() if k.lower() in ('content-type','content-encoding','content-length')},
      'rawLineCount':len(body.splitlines()),'parsedMessages':len(msgs),'depthMessages':len(depth),'snapshotMessages':len(snaps),
      'depthWithPu':pu_count,'puCoverage':(pu_count/len(depth) if depth else 0.0),
      'continuityChecks':len(continuity),'continuityPassRate':(sum(continuity)/len(continuity) if continuity else None),
      'sampleDepthKeys':sorted(sample_depth.keys()) if sample_depth else None,
      'sampleSnapshotKeys':sorted(sample_snap.keys()) if sample_snap else None,
      'sampleDepth':sample_depth,
      'sampleSnapshot':sample_snap,
      'canonicalReady':bool(depth and snaps and pu_count==len(depth) and (not continuity or all(continuity))),
    }
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
