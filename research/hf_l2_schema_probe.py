#!/usr/bin/env python3
import json, pathlib, urllib.request
import pyarrow.parquet as pq
BASE='https://huggingface.co/datasets/MaximumLeverage/crypto-lob-stream/resolve/main/'
FILES=[
 'depth/binance/SOLUSDT/2026-07.parquet',
 'snapshots/binance/SOLUSDT/2026-07.parquet',
 'trades/binance/SOLUSDT/2026-07.parquet',
]
out=[]
for rel in FILES:
    p=pathlib.Path('/tmp')/pathlib.Path(rel).name.replace('.parquet','-'+rel.split('/')[0]+'.parquet')
    req=urllib.request.Request(BASE+rel,headers={'User-Agent':'tst-l2-schema-probe/1.0'})
    with urllib.request.urlopen(req,timeout=120) as r, p.open('wb') as f:
        while True:
            b=r.read(1024*1024)
            if not b: break
            f.write(b)
    pf=pq.ParquetFile(p)
    table=pf.read_row_group(0)
    out.append({'path':rel,'bytes':p.stat().st_size,'rows':pf.metadata.num_rows,'rowGroups':pf.metadata.num_row_groups,'schema':str(pf.schema_arrow),'sample':table.slice(0,3).to_pylist()})
print(json.dumps(out,indent=2,default=str))
