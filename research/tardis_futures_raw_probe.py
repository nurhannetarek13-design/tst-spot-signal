#!/usr/bin/env python3
"""Probe Tardis raw Binance USD-M Futures feed for canonical U/u/pu fields.
Uses documented minute-slice API: from=YYYY-MM-DD, offset=N, lowercase symbols.
Research-only diagnostic. No trading actions.
"""
from __future__ import annotations
import argparse, gzip, json, urllib.parse, urllib.request

BASE='https://api.tardis.dev/v1/data-feeds/binance-futures'

def fetch_channel(symbol, date, offset, channel):
    filters=json.dumps([{'channel':channel,'symbols':[symbol.lower()]}], separators=(',',':'))
    qs=urllib.parse.urlencode({'from':date,'filters':filters,'offset':str(offset)})
    req=urllib.request.Request(
        BASE+'?'+qs,
        headers={'User-Agent':'tst-tardis-probe/2.0','Accept-Encoding':'gzip'}
    )
    with urllib.request.urlopen(req,timeout=180) as r:
        raw=r.read(); headers=dict(r.headers)
    if headers.get('Content-Encoding','').lower()=='gzip':
        raw=gzip.decompress(raw)
    return raw.decode('utf-8','replace'), headers

def parse_lines(body):
    out=[]
    for line in body.splitlines():
        line=line.strip()
        if not line: continue
        # Tardis raw feed: local timestamp followed by exchange-native JSON.
        candidates=[line]
        if ' ' in line and not line.startswith('{'):
            candidates.insert(0,line.split(' ',1)[1])
        obj=None
        for c in candidates:
            try: obj=json.loads(c); break
            except Exception: pass
        if obj is not None: out.append(obj)
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--symbol',default='BTCUSDT')
    ap.add_argument('--date',default='2020-02-01')
    ap.add_argument('--offset',type=int,default=0)
    a=ap.parse_args()

    depth_body,dh=fetch_channel(a.symbol,a.date,a.offset,'depth')
    snap_body,sh=fetch_channel(a.symbol,a.date,a.offset,'depthSnapshot')
    depth_all=parse_lines(depth_body); snap_all=parse_lines(snap_body)
    depth=[o for o in depth_all if isinstance(o,dict) and ((o.get('e')=='depthUpdate') or {'U','u'}.issubset(o))]
    snaps=[o for o in snap_all if isinstance(o,dict) and 'lastUpdateId' in o and ('bids' in o or 'asks' in o)]

    pu_count=sum('pu' in o for o in depth)
    checks=[]; prev=None
    for o in depth[:5000]:
        if prev is not None and 'pu' in o and 'u' in prev:
            checks.append(int(o['pu'])==int(prev['u']))
        prev=o

    sd=depth[0] if depth else None; ss=snaps[0] if snaps else None
    result={
      'authorization':'RESEARCH_ONLY','symbol':a.symbol,'date':a.date,'offset':a.offset,
      'depthRawLines':len(depth_body.splitlines()),'snapshotRawLines':len(snap_body.splitlines()),
      'depthMessages':len(depth),'snapshotMessages':len(snaps),
      'depthWithPu':pu_count,'puCoverage':pu_count/len(depth) if depth else 0.0,
      'continuityChecks':len(checks),'continuityPassRate':sum(checks)/len(checks) if checks else None,
      'sampleDepthKeys':sorted(sd.keys()) if sd else None,
      'sampleSnapshotKeys':sorted(ss.keys()) if ss else None,
      'sampleDepth':sd,'sampleSnapshot':ss,
      'canonicalReady':bool(depth and snaps and pu_count==len(depth) and (not checks or all(checks)))
    }
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
