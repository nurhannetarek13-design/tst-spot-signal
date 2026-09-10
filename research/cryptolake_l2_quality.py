#!/usr/bin/env python3
from __future__ import annotations
import io, json, math, pathlib, urllib.request, urllib.parse
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO='delmiron27/cryptolake-binance-futures-sol'
DATE='2025-01-15'
SYMBOL='SOL-USDT-PERP'
BASE=f'https://huggingface.co/datasets/{REPO}/resolve/main/'
HEADERS={'User-Agent':'tst-cryptolake-quality/1.0'}


def dl(rel,out):
    p=pathlib.Path(out); p.parent.mkdir(parents=True,exist_ok=True)
    req=urllib.request.Request(BASE+rel,headers=HEADERS)
    with urllib.request.urlopen(req,timeout=240) as r,p.open('wb') as f:
        while True:
            b=r.read(8*1024*1024)
            if not b: break
            f.write(b)
    return p

def schema_dict(path):
    pf=pq.ParquetFile(path)
    return {'rows':pf.metadata.num_rows,'rowGroups':pf.num_row_groups,'schema':str(pf.schema_arrow)}

def find_time_col(cols):
    candidates=['timestamp','ts','time','received_time','exchange_time','timestamp_ns','timestamp_ms']
    low={c.lower():c for c in cols}
    for c in candidates:
        if c in low:return low[c]
    for c in cols:
        if 'time' in c.lower():return c
    return None

def summarize_time(path,col):
    pf=pq.ParquetFile(path); vals=[]; n=0
    for b in pf.iter_batches(batch_size=200000,columns=[col]):
        a=b.column(0).to_pandas(); n+=len(a)
        if np.issubdtype(a.dtype,np.datetime64):
            x=pd.to_datetime(a,utc=True,errors='coerce')
        else:
            y=pd.to_numeric(a,errors='coerce')
            med=float(y.dropna().median()) if y.notna().any() else 0
            unit='ns' if med>1e17 else ('us' if med>1e14 else ('ms' if med>1e11 else 's'))
            x=pd.to_datetime(y,unit=unit,utc=True,errors='coerce')
        vals.append(x.dropna())
    if not vals:return {'nonNull':0}
    s=pd.concat(vals,ignore_index=True).sort_values().drop_duplicates()
    d=s.diff().dt.total_seconds().dropna()
    return {'nonNull':int(len(s)),'min':str(s.min()),'max':str(s.max()),'spanHours':float((s.max()-s.min()).total_seconds()/3600),'medianGapSec':float(d.median()) if len(d) else None,'p95GapSec':float(d.quantile(.95)) if len(d) else None,'maxGapSec':float(d.max()) if len(d) else None}

def main():
    root=pathlib.Path('/tmp/cryptolake-sol')
    book_rel=f'raw/book/exchange=BINANCE_FUTURES/symbol={SYMBOL}/dt={DATE}/1.snappy.parquet'
    trade_rel=f'raw/trades/exchange=BINANCE_FUTURES/symbol={SYMBOL}/dt={DATE}/1.snappy.parquet'
    out={'repo':REPO,'date':DATE,'bookRel':book_rel,'tradeRel':trade_rel}
    bp=dl(book_rel,root/'book.parquet'); out['bookBytes']=bp.stat().st_size; out['book']=schema_dict(bp)
    try:
        tp=dl(trade_rel,root/'trades.parquet'); out['tradeBytes']=tp.stat().st_size; out['trades']=schema_dict(tp)
    except Exception as e:
        out['trades']={'downloadError':str(e)}; tp=None
    bcols=pq.ParquetFile(bp).schema_arrow.names; tc=find_time_col(bcols); out['bookTimeCol']=tc
    if tc: out['bookTime']=summarize_time(bp,tc)
    if tp:
        tcols=pq.ParquetFile(tp).schema_arrow.names; tt=find_time_col(tcols); out['tradeTimeCol']=tt
        if tt: out['tradeTime']=summarize_time(tp,tt)
    print(json.dumps(out,indent=2))

if __name__=='__main__':main()
