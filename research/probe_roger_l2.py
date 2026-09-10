#!/usr/bin/env python3
from huggingface_hub import hf_hub_download
import pyarrow.parquet as pq
import json
REPO='rogerdehe/mktdata-binance-2026'
SYMS=['BTCUSDT-PERP','ETHUSDT-PERP','SOLUSDT-PERP']
DATES=['20260716','20260717','20260718']
out=[]
for s in SYMS:
  for d in DATES:
    month=d[:4]+'-'+d[4:6]
    for kind in ('deltas','trades'):
      fn=f'{s}/{month}/{s}_{d}_{kind}.parquet'
      try:
        p=hf_hub_download(REPO,fn,repo_type='dataset')
        pf=pq.ParquetFile(p)
        out.append({'symbol':s,'date':d,'kind':kind,'ok':True,'rows':pf.metadata.num_rows,'schema':pf.schema_arrow.names,'file':fn})
      except Exception as e:
        out.append({'symbol':s,'date':d,'kind':kind,'ok':False,'error':type(e).__name__+': '+str(e)[:220],'file':fn})
print(json.dumps(out,indent=2))
