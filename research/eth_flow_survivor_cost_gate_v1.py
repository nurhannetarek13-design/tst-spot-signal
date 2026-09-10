#!/usr/bin/env python3
from __future__ import annotations
import json, pathlib
from datetime import datetime
import pandas as pd
from research.binance_vision_historical_feature_layer import build
from research.historical_flow_raw_edge_scan_v1 import prepare,event_mask,metrics

AUTHORIZATION='RESEARCH_ONLY'
NORMAL_COST=0.0028
STRESS_COST=0.0050
MIN_EVENTS=20

def gate(m):
 return bool(m.get('n',0)>=MIN_EVENTS and m.get('mean',-1)>0 and m.get('median',-1)>0 and m.get('hitRate',0)>0.55 and m.get('profitFactor',0)>=1.20)

def main():
 raw,meta=build('ETHUSDT',datetime(2025,1,1).date(),datetime(2025,1,28).date(),'5min',True)
 if meta['failures']: raise RuntimeError(meta['failures'][:5])
 x=prepare(raw); x=x[x.ts>=pd.Timestamp('2025-01-15',tz='UTC')].copy()
 mask=event_mask(x,'flow_imbalance_quote','LOW')
 gross=pd.to_numeric(x.loc[mask,'fwd_15m'],errors='coerce').dropna()
 normal=gross-NORMAL_COST; stress=gross-STRESS_COST
 gm=metrics(gross); nm=metrics(normal); sm=metrics(stress)
 r={'engine':'ETH_FLOW_SURVIVOR_COST_GATE_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'candidate':{'symbol':'ETHUSDT','feature':'flow_imbalance_quote','side':'LOW','threshold':'z <= -2.0','horizon':'15m'},'selectionWindow':'2025-01-01..2025-01-14','evaluationWindow':'2025-01-15..2025-01-28','costs':{'normalRoundTrip':NORMAL_COST,'stressRoundTrip':STRESS_COST},'gross':{**gm,'pass':gate(gm)},'normalNet':{**nm,'pass':gate(nm)},'stressNet':{**sm,'pass':gate(sm)},'productionCandidate':bool(gate(nm) and gate(sm))}
 p=pathlib.Path('artifacts/eth-flow-survivor-cost-gate-v1.json'); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(r,indent=2,sort_keys=True)); print(json.dumps(r,separators=(',',':')))
if __name__=='__main__': main()
