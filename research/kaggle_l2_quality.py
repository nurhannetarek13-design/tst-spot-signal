#!/usr/bin/env python3
import io,json,urllib.request,zipfile,csv,statistics,datetime
SETS=[('aug','fast42/btcusdt-l2-order-book-binance-128-188'),('sep','fast42/btcusdt-l2-order-book-binance-179-279')]
out=[]
for label,slug in SETS:
    raw=urllib.request.urlopen(urllib.request.Request(f'https://www.kaggle.com/api/v1/datasets/download/{slug}',headers={'User-Agent':'Mozilla/5.0'}),timeout=90).read()
    z=zipfile.ZipFile(io.BytesIO(raw)); all_ts=[]; days=[]
    for n in sorted(z.namelist()):
        if not n.endswith('.csv'): continue
        text=io.TextIOWrapper(z.open(n),'utf-8')
        r=csv.DictReader(text); ts=[]; counts={}
        for row in r:
            t=int(row['timestamp']); ts.append(t); counts[t]=counts.get(t,0)+1
        uniq=sorted(counts); gaps=[(b-a)/1000 for a,b in zip(uniq,uniq[1:])]
        complete=sum(1 for t,c in counts.items() if c>=20)
        days.append({'file':n,'rows':len(ts),'snapshots':len(uniq),'completeTop10Snapshots':complete,'firstTs':uniq[0] if uniq else None,'lastTs':uniq[-1] if uniq else None,'medianGapSec':statistics.median(gaps) if gaps else None,'p95GapSec':sorted(gaps)[int(.95*(len(gaps)-1))] if gaps else None,'maxGapSec':max(gaps) if gaps else None})
        all_ts.extend(uniq)
    u=sorted(set(all_ts)); gaps=[(b-a)/1000 for a,b in zip(u,u[1:])]
    out.append({'label':label,'slug':slug,'totalSnapshots':len(u),'medianGapSec':statistics.median(gaps) if gaps else None,'days':days})
print(json.dumps(out,indent=2))
