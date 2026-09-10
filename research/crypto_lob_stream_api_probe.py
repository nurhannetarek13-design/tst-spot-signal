#!/usr/bin/env python3
import inspect, json, importlib.metadata as md
import crypto_lob_stream
out={
 'module':crypto_lob_stream.__file__,
 'version':md.version('crypto-lob-stream'),
 'metadata':{k:md.metadata('crypto-lob-stream').get(k) for k in ['Name','Version','Summary','Home-page','Project-URL','Author']},
}
for name in ['reconstruct','replay','load']:
 obj=getattr(crypto_lob_stream,name,None)
 if obj is not None:
  out[name]={'signature':str(inspect.signature(obj)),'doc':inspect.getdoc(obj),'source':inspect.getsource(obj)}
print(json.dumps(out,indent=2,default=str))
