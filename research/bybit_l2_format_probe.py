#!/usr/bin/env python3
import io,json,urllib.request,zipfile
URL='https://quote-saver.bycsi.com/orderbook/linear/SOLUSDT/2025-01-15_SOLUSDT_ob500.data.zip'
req=urllib.request.Request(URL,headers={'User-Agent':'Mozilla/5.0'})
with urllib.request.urlopen(req,timeout=180) as r:data=r.read()
z=zipfile.ZipFile(io.BytesIO(data));names=z.namelist();out={'zipBytes':len(data),'members':names[:10],'memberCount':len(names),'samples':[]}
for name in names[:1]:
    with z.open(name) as f:
        for _ in range(8):
            line=f.readline()
            if not line:break
            out['samples'].append(line.decode('utf-8','replace').rstrip()[:1000])
print(json.dumps(out,indent=2))
