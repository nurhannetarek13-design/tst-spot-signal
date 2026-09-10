#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import pandas as pd
from research.binance_vision_historical_feature_layer import build
from research.gate_d_event_discovery_v1 import prepare,HOURS,AUTH
from research.gate_d_event_chunk_v2 import EVENTS,samples

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--symbol',required=True);ap.add_argument('--offset-days',type=int,required=True);ap.add_argument('--core-days',type=int,default=30);ap.add_argument('--out',required=True);a=ap.parse_args()
    sym=a.symbol.upper();latest=(datetime.now(timezone.utc)-timedelta(days=2)).date();core_end=latest-timedelta(days=a.offset_days);core_start=core_end-timedelta(days=a.core_days-1)
    load_start=core_start-timedelta(days=2);load_end=min(latest,core_end+timedelta(days=3))
    d,_=build(sym,load_start,load_end,'15min',True);d['symbol']=sym;d=prepare(d)
    payload={'engine':'GATE_D_EVENT_SYMBOL_CHUNK_V3','authorization':AUTH,'liveTrading':False,'thresholdsFrozen':True,'symbol':sym,'offsetDays':a.offset_days,'coreStart':core_start.isoformat(),'coreEnd':core_end.isoformat(),'samples':{}}
    for ev in EVENTS:payload['samples'][ev]={str(h):samples(d,d[ev],h,core_start,core_end) for h in HOURS}
    p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload));print(json.dumps({'symbol':sym,'offsetDays':a.offset_days,'sampleCount':sum(len(v) for e in payload['samples'].values() for v in e.values())}))
if __name__=='__main__':main()
