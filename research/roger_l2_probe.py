#!/usr/bin/env python3
import json, urllib.request, urllib.parse
REPO='rogerdehe/mktdata-binance-2026'
HEADERS={'User-Agent':'Mozilla/5.0'}
def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=90) as r:
        return json.loads(r.read())
def tree(path):
    enc=urllib.parse.quote(path,safe='/')
    url=f'https://huggingface.co/api/datasets/{REPO}/tree/main/{enc}?recursive=false&expand=false&limit=1000'
    try:return fetch_json(url)
    except Exception as e:return {'error':str(e),'url':url}
out={}
for sym in ['BTCUSDT-PERP','ETHUSDT-PERP','SOLUSDT-PERP']:
    out[sym]={'months':tree(sym),'files':{}}
    months=out[sym]['months'] if isinstance(out[sym]['months'],list) else []
    for m in months:
        if m.get('type')=='directory' and m.get('path','').endswith(('2026-07','2026-08','2026-09')):
            p=m['path']; out[sym]['files'][p]=tree(p)
print(json.dumps(out,indent=2))
