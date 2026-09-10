#!/usr/bin/env python3
import json, pathlib, urllib.request
import pyarrow.parquet as pq
URL='https://huggingface.co/datasets/MaximumLeverage/crypto-lob-stream/resolve/main/depth/binance/SOLUSDT/2026-07.parquet'
p=pathlib.Path('/tmp/sol-depth.parquet')
if not p.exists():
 req=urllib.request.Request(URL,headers={'User-Agent':'tst-l2-audit/1.0'})
 with urllib.request.urlopen(req,timeout=180) as r,p.open('wb') as f:
  while True:
   b=r.read(4*1024*1024)
   if not b:break
   f.write(b)
pf=pq.ParquetFile(p)
rows=0; groups=0; backwards=0; same_id_noncontig=0; gap_groups=0; prev_key=None; prev_last=None; sizes=[]; cur_size=0
seen_recent=set()
for batch in pf.iter_batches(batch_size=250000,columns=['timestamp_ms','first_update_id','last_update_id']):
 d=batch.to_pydict()
 for ts,fi,li in zip(d['timestamp_ms'],d['first_update_id'],d['last_update_id']):
  rows+=1; key=(int(fi),int(li))
  if key!=prev_key:
   if prev_key is not None:sizes.append(cur_size)
   groups+=1; cur_size=1
   if prev_last is not None:
    if int(li)<prev_last:backwards+=1
    if int(fi)>prev_last+1:gap_groups+=1
   if key in seen_recent:same_id_noncontig+=1
   seen_recent.add(key)
   if len(seen_recent)>200000: seen_recent.clear()
   prev_key=key; prev_last=int(li)
  else:cur_size+=1
if cur_size:sizes.append(cur_size)
sizes_sorted=sorted(sizes)
def q(pct): return sizes_sorted[min(len(sizes_sorted)-1,int((len(sizes_sorted)-1)*pct))] if sizes_sorted else None
print(json.dumps({'rows':rows,'contiguousEventGroups':groups,'lastIdBackwardsAtGroupBoundary':backwards,'gapBoundariesByFileOrder':gap_groups,'repeatedEventKeyNonContiguousApprox':same_id_noncontig,'groupSize':{'min':min(sizes_sorted) if sizes_sorted else None,'median':q(.5),'p95':q(.95),'p99':q(.99),'max':max(sizes_sorted) if sizes_sorted else None}},indent=2))
