#!/usr/bin/env python3
from huggingface_hub import list_repo_files
import json
REPO='rogerdehe/mktdata-binance-2026'
files=list_repo_files(REPO,repo_type='dataset')
keys=('BTCUSDT','ETHUSDT','SOLUSDT')
sel=[f for f in files if any(k in f for k in keys) and ('2026-07' in f or '202607' in f)]
print(json.dumps({'totalFiles':len(files),'matched':sel[:500],'matchedCount':len(sel)},indent=2))
