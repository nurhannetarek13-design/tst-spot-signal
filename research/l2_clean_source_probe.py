#!/usr/bin/env python3
import json, urllib.request
BASE='https://huggingface.co/datasets/Goooddy/crypto-lob-stream/resolve/main/'
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']
PREFIXES=['depth/binance','snapshots/binance','trades/binance']
MONTH='2026-09'
out=[]
for s in SYMS:
    rec={'symbol':s,'month':MONTH,'files':{}}
    for p in PREFIXES:
        url=f'{BASE}{p}/{s}/{MONTH}.parquet'
        req=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'tst-l2-clean-probe/1.0'})
        try:
            with urllib.request.urlopen(req,timeout=60) as r:
                rec['files'][p]={'status':r.status,'length':int(r.headers.get('content-length') or 0),'url':url}
        except Exception as e:
            rec['files'][p]={'status':'ERROR','error':str(e),'url':url}
    out.append(rec)
print(json.dumps(out,indent=2))
