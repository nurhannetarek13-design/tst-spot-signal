#!/usr/bin/env python3
import json, re, urllib.request
BASE='https://quote-saver.bycsi.com/orderbook/linear'
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']
DATE='2025-01-15'
out=[]
for s in SYMS:
    rec={'symbol':s,'date':DATE,'listing':None,'matches':[]}
    url=f'{BASE}/{s}/'
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req,timeout=60) as r:
            html=r.read().decode('utf-8','replace')
        rec['listing']={'status':200,'bytes':len(html)}
        files=sorted(set(re.findall(r'href="([^"]+\.data\.zip)"',html)))
        cand=[f for f in files if f.startswith(DATE+'_')]
        for f in cand:
            furl=url+f
            try:
                h=urllib.request.Request(furl,method='HEAD',headers={'User-Agent':'Mozilla/5.0'})
                with urllib.request.urlopen(h,timeout=60) as rr:
                    rec['matches'].append({'file':f,'status':rr.status,'contentLength':int(rr.headers.get('content-length') or 0),'contentType':rr.headers.get('content-type')})
            except Exception as e:
                rec['matches'].append({'file':f,'status':'ERROR','error':str(e)})
    except Exception as e:
        rec['listing']={'status':'ERROR','error':str(e)}
    out.append(rec)
print(json.dumps(out,indent=2))
