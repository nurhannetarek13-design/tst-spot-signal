#!/usr/bin/env python3
"""Persistent research-only shadow state manager.
Consumes current public feature/score/decision snapshots, persists OPEN/CLOSED paper
trades across CI runs, labels mature trades with later public Binance Spot OHLC,
and emits a performance gate. No account access, keys, or order endpoints.
"""
from __future__ import annotations
import csv,json,pathlib,urllib.parse,urllib.request
from datetime import datetime,timezone
from research.shadow_labeler import label_one
from research.shadow_performance_gate import metrics,gate

BASE='https://data-api.binance.vision'
ROOT=pathlib.Path('validation/pro_stack')
STATE=ROOT/'state'/'shadow_trades.csv'
PERF=ROOT/'state'/'performance.json'
FIELDS=['trade_id','ts','symbol','status','entry_price','tp_pct','sl_pct','p_tp_before_sl','expected_net_pct','stake_usdt','model_type','close_reason','exit_price','tp_before_sl','net_return_pct','mfe_pct','mae_pct','holding_min']

def api(path):
    req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-shadow-state/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)

def bars(symbol,limit=64):
    q=urllib.parse.urlencode({'symbol':symbol,'interval':'15m','limit':limit});raw=api('/api/v3/klines?'+q);out=[]
    for b in raw:out.append({'ts':int(b[0]),'high':float(b[2]),'low':float(b[3]),'close':float(b[4])})
    return out

def load_state():
    if not STATE.exists():return []
    with STATE.open(encoding='utf-8') as f:return list(csv.DictReader(f))

def save_state(rows):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    with STATE.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');w.writeheader();w.writerows(rows)

def iso_from_ms(ms):return datetime.fromtimestamp(int(ms)/1000,tz=timezone.utc).isoformat().replace('+00:00','Z')

def add_current(rows):
    feat=json.load(open(ROOT/'feature_store.json'))['rows']; fmap={r['symbol']:r for r in feat}
    scores={r['symbol']:r for r in json.load(open(ROOT/'edge_scores.json'))['rows']}
    decisions=json.load(open(ROOT/'shadow_decisions.json'))['rows']; existing={r.get('trade_id') for r in rows}; added=0
    for d in decisions:
        if d.get('action')!='SHADOW_BUY':continue
        s=d['symbol']; f=fmap[s]; sc=scores[s]; tid=f"{s}:{int(f['ts'])}"
        if tid in existing:continue
        rows.append({'trade_id':tid,'ts':iso_from_ms(f['ts']),'symbol':s,'status':'OPEN','entry_price':f['price'],'tp_pct':1.2,'sl_pct':0.7,'p_tp_before_sl':sc['pTpBeforeSl'],'expected_net_pct':sc['expectedNetEdge']*100,'stake_usdt':d['stakeUSDT'],'model_type':'FROZEN_BASELINE_UNCALIBRATED','close_reason':'','exit_price':'','tp_before_sl':'','net_return_pct':'','mfe_pct':'','mae_pct':'','holding_min':''});existing.add(tid);added+=1
    return added

def label_open(rows):
    cache={};closed=0
    for i,r in enumerate(rows):
        if r.get('status')!='OPEN':continue
        s=r['symbol']
        if s not in cache:cache[s]=bars(s)
        z=label_one(r,cache[s],fee_roundtrip_pct=.28,timeout_min=240)
        if z.get('status')=='CLOSED':closed+=1
        rows[i]=z
    return closed

def main():
    rows=load_state();added=add_current(rows);closed=label_open(rows);save_state(rows);m=metrics(rows);g=gate(m)
    out={'engine':'SHADOW_FORWARD_STATE_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'modelStatus':'BASELINE_UNCALIBRATED','addedThisRun':added,'closedThisRun':closed,'openTrades':sum(r.get('status')=='OPEN' for r in rows),'closedTrades':sum(r.get('status')=='CLOSED' for r in rows),'metrics':m,'gate':g}
    PERF.write_text(json.dumps(out,indent=2));print(json.dumps(out))
if __name__=='__main__':main()
