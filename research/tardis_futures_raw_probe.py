#!/usr/bin/env python3
"""Probe Tardis raw Binance USD-M Futures feed for U/u/pu and snapshot fields.
Uses documented curl --compressed -g transport. Research-only diagnostic.
"""
from __future__ import annotations
import argparse, json, subprocess, urllib.parse

BASE='https://api.tardis.dev/v1/data-feeds/binance-futures'

def fetch_channel(symbol,date,offset,channel):
    filt=json.dumps([{'channel':channel,'symbols':[symbol.lower()]}],separators=(',',':'))
    url=f"{BASE}?from={urllib.parse.quote(date,safe=':-TZ')}&filters={urllib.parse.quote(filt,safe='[]{}\":,')}&offset={offset}"
    p=subprocess.run(['curl','--compressed','-sS','-g',url],capture_output=True,check=True)
    return p.stdout.decode('utf-8','replace')

def parse_lines(body):
    out=[]
    for line in body.splitlines():
        line=line.strip()
        if not line: continue
        i=line.find('{')
        if i<0: continue
        payload=line[i:]
        try: out.append(json.loads(payload))
        except Exception: pass
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--symbol',default='BTCUSDT'); ap.add_argument('--date',default='2021-09-01'); ap.add_argument('--offset',type=int,default=0); a=ap.parse_args()
    db=fetch_channel(a.symbol,a.date,a.offset,'depth'); sb=fetch_channel(a.symbol,a.date,a.offset,'depthSnapshot')
    da=parse_lines(db); sa=parse_lines(sb)
    depth=[o for o in da if isinstance(o,dict) and ((o.get('e')=='depthUpdate') or {'U','u'}.issubset(o))]
    snaps=[o for o in sa if isinstance(o,dict) and 'lastUpdateId' in o and ('bids' in o or 'asks' in o)]
    pu=sum('pu' in o for o in depth)
    checks=[]; prev=None
    for o in depth[:5000]:
        if prev is not None and 'pu' in o and 'u' in prev: checks.append(int(o['pu'])==int(prev['u']))
        prev=o
    sd=depth[0] if depth else None; ss=snaps[0] if snaps else None
    print(json.dumps({
      'authorization':'RESEARCH_ONLY','symbol':a.symbol,'date':a.date,'offset':a.offset,
      'depthRawLines':len(db.splitlines()),'snapshotRawLines':len(sb.splitlines()),
      'depthMessages':len(depth),'snapshotMessages':len(snaps),'depthWithPu':pu,
      'puCoverage':pu/len(depth) if depth else 0.0,
      'continuityChecks':len(checks),'continuityPassRate':sum(checks)/len(checks) if checks else None,
      'sampleDepthKeys':sorted(sd.keys()) if sd else None,'sampleSnapshotKeys':sorted(ss.keys()) if ss else None,
      'sampleDepth':sd,'sampleSnapshot':ss,
      'firstDepthRawPrefix':(db.splitlines()[0][:180] if db.splitlines() else None),
      'firstSnapshotRawPrefix':(sb.splitlines()[0][:180] if sb.splitlines() else None),
      'canonicalReady':bool(depth and snaps and pu==len(depth) and (not checks or all(checks)))
    },indent=2))

if __name__=='__main__': main()
