#!/usr/bin/env python3
import io,json,urllib.request,zipfile,csv
sym='SOLUSDT'; ds='2025-01-15'
url=f'https://data.binance.vision/data/spot/daily/aggTrades/{sym}/{sym}-aggTrades-{ds}.zip'
raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=120).read()
with zipfile.ZipFile(io.BytesIO(raw)) as z:
    n=z.namelist()[0]
    with z.open(n) as f:
        lines=[]
        for i,line in enumerate(f):
            lines.append(line.decode('utf-8','replace').strip())
            if i>=4:break
print(json.dumps({'url':url,'zipBytes':len(raw),'member':n,'firstLines':lines},indent=2))
