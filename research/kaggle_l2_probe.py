#!/usr/bin/env python3
import json, urllib.request, urllib.error
SETS=[
 ('aug','fast42/btcusdt-l2-order-book-binance-128-188'),
 ('sep','fast42/btcusdt-l2-order-book-binance-179-279'),
]
out=[]
for label,slug in SETS:
    owner,name=slug.split('/',1)
    rec={'label':label,'slug':slug}
    meta=f'https://www.kaggle.com/api/v1/datasets/view/{owner}/{name}'
    try:
        with urllib.request.urlopen(urllib.request.Request(meta,headers={'User-Agent':'Mozilla/5.0'}),timeout=60) as r:
            body=r.read(200000)
            rec['metadata']={'status':r.status,'bytesRead':len(body)}
            try:
                j=json.loads(body)
                rec['metadata'].update({'title':j.get('title'),'totalBytes':j.get('totalBytes'),'versionNumber':j.get('currentVersionNumber'),'files':[{'name':x.get('name'),'totalBytes':x.get('totalBytes')} for x in (j.get('resources') or [])[:20]]})
            except Exception as e: rec['metadata']['parseError']=str(e)
    except Exception as e: rec['metadata']={'status':'ERROR','error':str(e)}

    url=f'https://www.kaggle.com/api/v1/datasets/download/{owner}/{name}'
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Range':'bytes=0-4095','Accept':'*/*'})
    try:
        with urllib.request.urlopen(req,timeout=90) as r:
            chunk=r.read(4096)
            rec['download']={'status':r.status,'bytesRead':len(chunk),'contentType':r.headers.get('content-type'),'contentLength':r.headers.get('content-length'),'contentRange':r.headers.get('content-range'),'finalUrl':r.geturl(),'magic':chunk[:8].hex()}
    except urllib.error.HTTPError as e:
        rec['download']={'status':e.code,'error':str(e),'body':e.read(500).decode('utf-8','replace')}
    except Exception as e: rec['download']={'status':'ERROR','error':str(e)}
    out.append(rec)
print(json.dumps(out,indent=2))
