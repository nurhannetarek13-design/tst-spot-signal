#!/usr/bin/env python3
import urllib.request
url='https://huggingface.co/datasets/MaximumLeverage/crypto-lob-stream/resolve/main/README.md'
text=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'tst-l2-readme/1.0'}),timeout=60).read().decode('utf-8','replace')
for line in text.splitlines():
    l=line.lower()
    if any(k in l for k in ['depth','snapshot','futures','spot','update','binance','reconstruct','gap','100ms','250ms']):
        print(line)
