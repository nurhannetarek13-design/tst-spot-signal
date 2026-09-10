#!/usr/bin/env python3
from __future__ import annotations
import json,pathlib,time
S=pathlib.Path('validation/pro_stack/edge_scores.json');OUT=pathlib.Path('validation/pro_stack/shadow_decisions.json')
MAX_POS=3;MAX_DAILY_LOSS=2.0;MAX_STAKE=10.0
def size(p,edge):
 if p>=.75 and edge>=.004:return 10.0
 if p>=.68 and edge>=.002:return 7.0
 if p>=.65 and edge>0:return 5.0
 return 0.0
def main():
 x=json.load(open(S));rows=[]
 for r in sorted(x['rows'],key=lambda z:z['expectedNetEdge'],reverse=True):
  stake=size(r['pTpBeforeSl'],r['expectedNetEdge']);allow=r['decision']=='SHADOW_BUY' and stake>0 and len([a for a in rows if a['action']=='SHADOW_BUY'])<MAX_POS
  rows.append({'symbol':r['symbol'],'action':'SHADOW_BUY' if allow else 'SKIP','stakeUSDT':stake if allow else 0.0,'pTpBeforeSl':r['pTpBeforeSl'],'expectedNetEdge':r['expectedNetEdge'],'hardStopPct':-0.007,'takeProfitPct':0.012,'reason':'EDGE_AND_RISK_PASS' if allow else 'EDGE_OR_CAP_FAIL'})
 o={'engine':'PRO_STACK_SHADOW_TRADER_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'maxDailyLossUSDT':MAX_DAILY_LOSS,'maxOpenPositions':MAX_POS,'rows':rows,'generatedAt':int(time.time())};OUT.write_text(json.dumps(o,indent=2));print(json.dumps(o))
if __name__=='__main__':main()
