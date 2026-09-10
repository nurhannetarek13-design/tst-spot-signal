#!/usr/bin/env python3
import json, os
from huggingface_hub import HfApi, hf_hub_download
import pyarrow.parquet as pq

REPO='predict-quant/binance-future-orderbook'
api=HfApi()
rows=[]
for item in api.list_repo_tree(REPO, repo_type='dataset', recursive=True, expand=True):
    path=getattr(item,'path',None)
    if not path or not path.endswith('.parquet'): continue
    if any(path.startswith(s+'/') for s in ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT']):
        rows.append({'path':path,'size':getattr(item,'size',None)})
rows=sorted(rows,key=lambda x:x['path'])
# choose smallest visible parquet for schema/cadence probe
chosen=min(rows,key=lambda x:(x['size'] if isinstance(x['size'],int) else 10**18,x['path'])) if rows else None
out={'repo':REPO,'fileCount':len(rows),'firstFiles':rows[:20],'chosen':chosen}
if chosen:
    local=hf_hub_download(REPO, chosen['path'], repo_type='dataset')
    pf=pq.ParquetFile(local)
    out['numRows']=pf.metadata.num_rows
    out['schema']=str(pf.schema_arrow)
    cols=pf.schema_arrow.names
    sample_cols=cols[:min(12,len(cols))]
    t=pf.read_row_group(0, columns=sample_cols)
    out['sample']=t.slice(0,min(5,t.num_rows)).to_pylist()
    # timestamp cadence if a likely timestamp column exists
    cand=[c for c in cols if 'time' in c.lower() or 'timestamp' in c.lower()]
    if cand:
        tc=cand[0]
        arr=pf.read(columns=[tc]).column(0).to_pylist()[:1000]
        vals=[]
        for v in arr:
            if v is None: continue
            try: vals.append(int(v.timestamp()*1000) if hasattr(v,'timestamp') else int(v))
            except Exception: pass
        if len(vals)>2:
            diffs=[b-a for a,b in zip(vals,vals[1:]) if b>a]
            if diffs: out['timestampColumn']=tc; out['medianPositiveDeltaRaw']=sorted(diffs)[len(diffs)//2]
print(json.dumps(out,indent=2,default=str))
