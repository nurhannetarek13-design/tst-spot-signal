#!/usr/bin/env python3
import json, urllib.request, urllib.parse
REPO='delmiron27/cryptolake-binance-futures-sol'
HEADERS={'User-Agent':'Mozilla/5.0'}
def getj(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=90) as r:return json.loads(r.read())
def tree(path='',recursive=False):
    enc=urllib.parse.quote(path,safe='/=')
    base=f'https://huggingface.co/api/datasets/{REPO}/tree/main'
    url=base+('/'+enc if enc else '')+f'?recursive={str(recursive).lower()}&expand=false&limit=1000'
    try:return getj(url)
    except Exception as e:return {'error':str(e),'url':url}
paths=['','raw','raw/book','raw/book/exchange=BINANCE_FUTURES','raw/book/exchange=BINANCE_FUTURES/symbol=SOL-USDT-PERP']
out={p:tree(p) for p in paths}
# If day directories are visible, list first/last 5 and one day's files.
last=out[paths[-1]] if isinstance(out[paths[-1]],list) else []
days=[x for x in last if x.get('type')=='directory']
out['day_summary']={'count':len(days),'first':days[:5],'last':days[-5:]}
for x in (days[:1]+days[-1:]):
    out['files:'+x['path']]=tree(x['path'])
print(json.dumps(out,indent=2)[:150000])
