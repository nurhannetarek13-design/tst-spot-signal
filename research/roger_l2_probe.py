#!/usr/bin/env python3
import json, urllib.request
BASE='https://huggingface.co/datasets/rogerdehe/mktdata-binance-2026/resolve/main/'
items=[]
for sym in ['BTCUSDT-PERP','ETHUSDT-PERP','SOLUSDT-PERP']:
  for date in ['20260716','20260717','20260718']:
    month='2026-07'
    for kind in ['deltas','trades','control']:
      ext='parquet' if kind!='control' else 'jsonl'
      path=f'{sym}/{month}/{sym}_{date}_{kind}.{ext}'
      url=BASE+path
      try:
        req=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req,timeout=60) as r:
          items.append({'symbol':sym,'date':date,'kind':kind,'status':r.status,'length':int(r.headers.get('content-length') or 0),'url':r.geturl()})
      except Exception as e:
        items.append({'symbol':sym,'date':date,'kind':kind,'status':'ERROR','error':str(e)})
print(json.dumps(items,indent=2))
