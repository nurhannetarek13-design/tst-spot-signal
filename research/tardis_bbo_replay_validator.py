#!/usr/bin/env python3
"""Validate Binance USD-M L2 replay against independent bookTicker BBO messages.
Research-only. No trading actions.
"""
from __future__ import annotations
import argparse, json, subprocess, urllib.parse
from decimal import Decimal

BASE='https://api.tardis.dev/v1/data-feeds/binance-futures'
AUTHORIZATION='RESEARCH_ONLY'


def fetch(symbol,date,offset,channel):
    filt=json.dumps([{'channel':channel,'symbols':[symbol.lower()]}],separators=(',',':'))
    url=f"{BASE}?from={urllib.parse.quote(date,safe=':-TZ')}&filters={urllib.parse.quote(filt,safe='[]{}\":,')}&offset={offset}"
    p=subprocess.run(['curl','--compressed','-sS','-g',url],capture_output=True,check=True)
    return p.stdout.decode('utf-8','replace')


def parse(body):
    out=[]
    for raw in body.splitlines():
        raw=raw.strip()
        if not raw: continue
        i=raw.find('{')
        if i<0: continue
        try: x=json.loads(raw[i:])
        except json.JSONDecodeError: continue
        if isinstance(x,dict) and isinstance(x.get('data'),dict): x=x['data']
        if isinstance(x,dict): out.append(x)
    return out


def q(v): return Decimal(str(v))


def apply(book, rows):
    for p,qty in rows:
        p=q(p); qty=q(qty)
        if qty==0: book.pop(p,None)
        else: book[p]=qty


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--symbol',default='BTCUSDT',choices=['BTCUSDT','ETHUSDT','SOLUSDT'])
    ap.add_argument('--date',default='2021-09-01')
    ap.add_argument('--offset',type=int,default=0)
    a=ap.parse_args()

    depths=[x for x in parse(fetch(a.symbol,a.date,a.offset,'depth')) if x.get('e')=='depthUpdate' or {'U','u'}.issubset(x)]
    snaps=[x for x in parse(fetch(a.symbol,a.date,a.offset,'depthSnapshot')) if 'lastUpdateId' in x and 'bids' in x and 'asks' in x]
    tickers=[x for x in parse(fetch(a.symbol,a.date,a.offset,'bookTicker')) if 'u' in x and {'b','B','a','A'}.issubset(x)]
    if not depths or not snaps:
        print(json.dumps({'authorization':AUTHORIZATION,'liveTrading':False,'status':'NO_INPUT','depths':len(depths),'snapshots':len(snaps),'bookTickers':len(tickers)},indent=2)); return

    s=snaps[0]; sid=int(s['lastUpdateId'])
    bids={q(p):q(v) for p,v in s['bids'] if q(v)>0}
    asks={q(p):q(v) for p,v in s['asks'] if q(v)>0}
    ticker_by_u={}
    for t in tickers: ticker_by_u.setdefault(int(t['u']),[]).append(t)

    started=False; prev_u=None; continuity=0; applied=0; matched=0; mismatches=[]; first_bridge=None
    for e in depths:
        U,u=int(e['U']),int(e['u'])
        if u<=sid: continue
        if not started:
            if U<=sid+1<=u:
                started=True; first_bridge={'U':U,'u':u,'pu':int(e['pu']) if 'pu' in e else None}
            elif U>sid+1:
                print(json.dumps({'authorization':AUTHORIZATION,'liveTrading':False,'status':'FIRST_EVENT_GAP','symbol':a.symbol,'snapshotId':sid,'expected':sid+1,'U':U,'u':u},indent=2)); return
            else: continue
        else:
            if 'pu' not in e or int(e['pu'])!=int(prev_u):
                print(json.dumps({'authorization':AUTHORIZATION,'liveTrading':False,'status':'PU_GAP','symbol':a.symbol,'prev_u':prev_u,'pu':e.get('pu'),'U':U,'u':u},indent=2)); return
            continuity+=1

        apply(bids,e.get('b',[])); apply(asks,e.get('a',[])); prev_u=u; applied+=1
        if not bids or not asks or max(bids)>=min(asks):
            print(json.dumps({'authorization':AUTHORIZATION,'liveTrading':False,'status':'INVALID_BOOK','symbol':a.symbol,'U':U,'u':u},indent=2)); return

        if u in ticker_by_u:
            bp=max(bids); apx=min(asks); bv=bids[bp]; av=asks[apx]
            ok=False; expected=[]
            for t in ticker_by_u[u]:
                ref=(q(t['b']),q(t['B']),q(t['a']),q(t['A']))
                expected.append([str(x) for x in ref])
                if (bp,bv,apx,av)==ref: ok=True; break
            matched+=1
            if not ok:
                mismatches.append({'u':u,'replayed':[str(bp),str(bv),str(apx),str(av)],'bookTicker':expected[:3]})
                if len(mismatches)>=5: break

    status='PASS' if started and matched>0 and not mismatches else ('NO_MATCHED_IDS' if started and not mismatches else 'MISMATCH')
    print(json.dumps({
      'authorization':AUTHORIZATION,'liveTrading':False,'status':status,'symbol':a.symbol,'date':a.date,'offset':a.offset,
      'snapshotId':sid,'depthMessages':len(depths),'bookTickerMessages':len(tickers),'firstBridge':first_bridge,
      'eventsApplied':applied,'continuityChecks':continuity,'matchedUpdateIds':matched,'mismatchCount':len(mismatches),
      'firstMismatches':mismatches,'canonicalBboReady':status=='PASS'
    },indent=2))

if __name__=='__main__': main()
