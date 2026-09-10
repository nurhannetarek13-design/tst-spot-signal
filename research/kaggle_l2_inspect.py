#!/usr/bin/env python3
import io,json,urllib.request,zipfile,csv
SETS=[('aug','fast42/btcusdt-l2-order-book-binance-128-188'),('sep','fast42/btcusdt-l2-order-book-binance-179-279')]
out=[]
for label,slug in SETS:
    url=f'https://www.kaggle.com/api/v1/datasets/download/{slug}'
    raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=90).read()
    z=zipfile.ZipFile(io.BytesIO(raw))
    rec={'label':label,'slug':slug,'zipBytes':len(raw),'members':[]}
    for n in z.namelist():
        if n.endswith('/'):
            continue
        b=z.read(n)
        text=b[:20000].decode('utf-8','replace')
        lines=text.splitlines()
        rec['members'].append({'name':n,'bytes':len(b),'firstLines':lines[:5]})
    out.append(rec)
print(json.dumps(out,indent=2))
