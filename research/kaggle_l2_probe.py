#!/usr/bin/env python3
import json, urllib.request
SETS=[
 ('aug','fast42/btcusdt-l2-order-book-binance-128-188'),
 ('sep','fast42/btcusdt-l2-order-book-binance-179-279'),
]
out=[]
for label,slug in SETS:
    url=f'https://www.kaggle.com/api/v1/datasets/download/{slug}'
    req=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'tst-kaggle-l2-probe/1.0'})
    try:
        with urllib.request.urlopen(req,timeout=60) as r:
            out.append({'label':label,'slug':slug,'status':r.status,'length':int(r.headers.get('content-length') or 0),'final_url':r.geturl()})
    except Exception as e:
        out.append({'label':label,'slug':slug,'status':'ERROR','error':str(e)})
print(json.dumps(out,indent=2))
