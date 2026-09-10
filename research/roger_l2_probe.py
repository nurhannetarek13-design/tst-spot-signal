#!/usr/bin/env python3
import json, urllib.request, urllib.parse
REPO='rogerdehe/mktdata-binance-2026'
headers={'User-Agent':'Mozilla/5.0'}
def fetch_json(url):
    req=urllib.request.Request(url,headers=headers)
    with urllib.request.urlopen(req,timeout=90) as r:
        return json.loads(r.read())
def get_tree(path=''):
    encoded=urllib.parse.quote(path,safe='/')
    base='https://huggingface.co/api/datasets/'+REPO+'/tree/main'
    url=base+('/'+encoded if encoded else '')+'?recursive=false&expand=false&limit=1000'
    try:
        return fetch_json(url)
    except Exception as e:
        return {'error':str(e),'url':url}
root=get_tree('')
out={'root':root,'children':{}}
if isinstance(root,list):
    for item in root:
        if item.get('type')=='directory':
            p=item.get('path')
            out['children'][p]=get_tree(p)
print(json.dumps(out,indent=2)[:150000])
