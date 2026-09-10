#!/usr/bin/env python3
from huggingface_hub import list_repo_files
import json
REPO='rogerdehe/mktdata-binance-2026'
files=list_repo_files(REPO,repo_type='dataset')
btc=[f for f in files if 'BTC' in f.upper()]
eth=[f for f in files if 'ETH' in f.upper()]
sol=[f for f in files if 'SOL' in f.upper()]
print(json.dumps({'totalFiles':len(files),'first':files[:120],'last':files[-120:],'btc':btc[:200],'eth':eth[:100],'sol':sol[:100],'btcCount':len(btc),'ethCount':len(eth),'solCount':len(sol)},indent=2))
