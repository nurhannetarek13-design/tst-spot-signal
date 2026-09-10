#!/usr/bin/env python3
import json, urllib.request
DATASET='MaximumLeverage/crypto-lob-stream'
UA='tst-hf-l2-probe/1.1'

def get_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=60) as r:
        return json.load(r)

info=get_json('https://huggingface.co/api/datasets/'+DATASET)
tree=get_json('https://huggingface.co/api/datasets/'+DATASET+'/tree/main?recursive=true&expand=true')
files=[]
for s in tree:
    if s.get('type')=='file':
        files.append({'path':s.get('path'),'size':s.get('size'),'oid':s.get('oid')})
print(json.dumps({'dataset':DATASET,'private':info.get('private'),'gated':info.get('gated'),'downloads':info.get('downloads'),'files':files},indent=2))
