#!/usr/bin/env python3
import json, urllib.request
DATASET='MaximumLeverage/crypto-lob-stream'
UA='tst-hf-l2-probe/1.0'

def get_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=60) as r:
        return json.load(r)

info=get_json('https://huggingface.co/api/datasets/'+DATASET)
files=[]
for s in info.get('siblings',[]):
    files.append({'rfilename':s.get('rfilename'),'size':s.get('size'),'blob_id':s.get('blobId'),'lfs':s.get('lfs')})
print(json.dumps({'dataset':DATASET,'private':info.get('private'),'gated':info.get('gated'),'downloads':info.get('downloads'),'files':files},indent=2))
