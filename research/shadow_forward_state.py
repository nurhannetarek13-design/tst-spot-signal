#!/usr/bin/env python3
"""Persistent research-only Gate A Spot baseline state manager.
Consumes frozen Spot-only feature/score/decision snapshots, persists OPEN/CLOSED
paper trades across CI runs, labels them with later public Binance Spot OHLC,
and emits milestone/performance status. No account access, keys, or orders.
"""
from __future__ import annotations
import csv,json,pathlib,urllib.parse,urllib.request
from datetime import datetime,timezone
from research.shadow_labeler import label_one,DEFAULT_COSTS,total_cost_pct
from research.shadow_performance_gate import metrics,gate,milestone

BASE='https://data-api.binance.vision'; ROOT=pathlib.Path('validation/pro_stack'); EXP=pathlib.Path('research/spot_baseline_gate_a.json')
STATE=ROOT/'state'/'shadow_trades.csv'; PERF=ROOT/'state'/'performance.json'
FIELDS=['experiment','trade_id','ts','symbol','status','entry_price','tp_pct','sl_pct','p_tp_before_sl','expected_net_pct','stake_usdt','model_type','close_reason','exit_price','tp_before_sl','gross_return_pct','fee_roundtrip_pct','spread_roundtrip_pct','slippage_roundtrip_pct','total_cost_pct','net_return_pct','mfe_pct','mae_pct','holding_min']

def api(path):
    req=urllib.request.Request(BASE+path,headers={'User-Agent':'tst-shadow-gate-a/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def bars(symbol,limit=64):
    q=urllib.parse.urlencode({'symbol':symbol,'interval':'15m','limit':limit});raw=api('/api/v3/klines?'+q)
    return [{'ts':int(b[0]),'high':float(b[2]),'low':float(b[3]),'close':float(b[4])} for b in raw]
def load_state(experiment):
    if not STATE.exists():return []
    with STATE.open(encoding='utf-8') as f:rows=list(csv.DictReader(f))
    bad=[r for r in rows if r.get('experiment') not in ('',experiment)]
    if bad:raise RuntimeError('STATE_EXPERIMENT_MISMATCH')
    for r in rows:
        if not r.get('experiment'):r['experiment']=experiment
    return rows
def save_state(rows):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    with STATE.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');w.writeheader();w.writerows(rows)
def iso_from_ms(ms):return datetime.fromtimestamp(int(ms)/1000,tz=timezone.utc).isoformat().replace('+00:00','Z')
def add_current(rows,cfg):
    experiment=cfg['experiment']; feat=json.load(open(ROOT/'feature_store.json'))['rows'];fmap={r['symbol']:r for r in feat}
    scores={r['symbol']:r for r in json.load(open(ROOT/'edge_scores.json'))['rows']}; decisions=json.load(open(ROOT/'shadow_decisions.json'))['rows'];existing={r.get('trade_id') for r in rows};added=0
    for d in decisions:
        if d.get('action')!='SHADOW_BUY':continue
        s=d['symbol'];f=fmap[s];sc=scores[s];tid=f"{experiment}:{s}:{int(f['ts'])}"
        if tid in existing:continue
        rows.append({'experiment':experiment,'trade_id':tid,'ts':iso_from_ms(f['ts']),'symbol':s,'status':'OPEN','entry_price':f['price'],'tp_pct':cfg['barrier']['takeProfitPct'],'sl_pct':cfg['barrier']['stopLossPct'],'p_tp_before_sl':sc['pTpBeforeSl'],'expected_net_pct':sc['expectedNetEdge']*100,'stake_usdt':d['stakeUSDT'],'model_type':cfg['modelStatus'],'close_reason':'','exit_price':'','tp_before_sl':'','gross_return_pct':'','fee_roundtrip_pct':'','spread_roundtrip_pct':'','slippage_roundtrip_pct':'','total_cost_pct':'','net_return_pct':'','mfe_pct':'','mae_pct':'','holding_min':''});existing.add(tid);added+=1
    return added
def label_open(rows,cfg):
    cache={};closed=0;c={'fee_roundtrip_pct':cfg['costModel']['feeRoundTripPct'],'spread_roundtrip_pct':cfg['costModel']['spreadEstimateRoundTripPct'],'slippage_roundtrip_pct':cfg['costModel']['slippageBufferRoundTripPct']}
    for i,r in enumerate(rows):
        if r.get('status')!='OPEN':continue
        s=r['symbol']
        if s not in cache:cache[s]=bars(s)
        z=label_one(r,cache[s],fee_roundtrip_pct=None,timeout_min=cfg['barrier']['timeoutMin'],costs=c)
        if z.get('status')=='CLOSED':closed+=1
        rows[i]=z
    return closed
def main():
    cfg=json.load(open(EXP));exp=cfg['experiment'];rows=load_state(exp);added=add_current(rows,cfg);closed=label_open(rows,cfg);save_state(rows);m=metrics(rows);g=gate(m);ms=milestone(m['n']);costs,total=total_cost_pct({'fee_roundtrip_pct':cfg['costModel']['feeRoundTripPct'],'spread_roundtrip_pct':cfg['costModel']['spreadEstimateRoundTripPct'],'slippage_roundtrip_pct':cfg['costModel']['slippageBufferRoundTripPct']})
    out={'engine':'SHADOW_FORWARD_STATE_GATE_A_V1','experiment':exp,'authorization':'RESEARCH_ONLY','liveTrading':False,'shadowLabelsRole':'PURE_VALIDATION','retrainingAllowed':False,'derivativesFeaturesAllowed':False,'modelStatus':cfg['modelStatus'],'costModel':{**costs,'total_roundtrip_pct':total},'addedThisRun':added,'closedThisRun':closed,'openTrades':sum(r.get('status')=='OPEN' for r in rows),'closedTrades':sum(r.get('status')=='CLOSED' for r in rows),'metrics':m,'milestone':ms,'gate':g}
    PERF.parent.mkdir(parents=True,exist_ok=True);PERF.write_text(json.dumps(out,indent=2));print(json.dumps(out))
if __name__=='__main__':main()
