#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, pathlib
from datetime import datetime
import pandas as pd
from research.binance_vision_historical_feature_layer import build
from research.historical_flow_raw_edge_scan_v1 import prepare, metrics, gate, event_mask

AUTHORIZATION='RESEARCH_ONLY'
FROZEN={
 'BTCUSDT':[('trade_count_z','HIGH','240m')],
 'ETHUSDT':[('flow_imbalance_quote','LOW','15m'),('flow_imbalance_quote','LOW','240m'),('flow_1h','HIGH','15m')],
 'SOLUSDT':[('flow_1h','LOW','15m')],
 'XRPUSDT':[('flow_1h','LOW','240m')],
 'DOGEUSDT':[('flow_imbalance_quote','LOW','60m')],
}
MIN_EVENTS=20

def main():
 p=argparse.ArgumentParser(); p.add_argument('--symbol',required=True); p.add_argument('--start',default='2025-01-01'); p.add_argument('--eval-start',default='2025-01-15'); p.add_argument('--end',default='2025-01-28'); p.add_argument('--output-dir',default='artifacts/historical-flow-frozen-retest'); a=p.parse_args()
 s=a.symbol.upper(); assert s in FROZEN
 raw,meta=build(s,datetime.strptime(a.start,'%Y-%m-%d').date(),datetime.strptime(a.end,'%Y-%m-%d').date(),'5min',True)
 if meta['failures']: raise RuntimeError(meta['failures'][:5])
 x=prepare(raw); x=x[x['ts']>=pd.Timestamp(a.eval_start,tz='UTC')].copy()
 out=[]
 for f,side,h in FROZEN[s]:
  m=event_mask(x,f,side); met=metrics(x.loc[m,'fwd_'+h]); out.append({'feature':f,'side':side,'horizon':h,'threshold':'z >= 2.0' if side=='HIGH' else 'z <= -2.0','metrics':{**met,'pass':gate(met,MIN_EVENTS)}})
 r={'engine':'HISTORICAL_FLOW_FROZEN_RETEST_V1','authorization':AUTHORIZATION,'liveTrading':False,'automaticPromotion':False,'selectionWindow':'2025-01-01..2025-01-14','evaluationWindow':{'start':a.eval_start,'end':a.end},'symbol':s,'frozenCandidates':FROZEN[s],'results':out,'passCount':sum(int(z['metrics']['pass']) for z in out),'allPass':all(z['metrics']['pass'] for z in out)}
 d=pathlib.Path(a.output_dir); d.mkdir(parents=True,exist_ok=True); path=d/f'{s}-frozen-retest-v1.json'; path.write_text(json.dumps(r,indent=2,sort_keys=True)); print(json.dumps(r,separators=(',',':')))
if __name__=='__main__': main()
