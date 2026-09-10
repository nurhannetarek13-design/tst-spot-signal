#!/usr/bin/env python3
"""Shadow forward engine.
Consumes unified features + model scores, applies portfolio risk, and records
paper decisions. It NEVER places orders and has no exchange credentials.
"""
from __future__ import annotations
import argparse, csv, json, pathlib
from research.unified_feature_store import normalize
from research.portfolio_risk import decide

FIELDS=[
 'ts','symbol','setup_type','regime','decision','reason','p_tp_before_sl',
 'expected_net_pct','stake_usdt','entry_price','tp_pct','sl_pct','status',
 'tp_before_sl','net_return_pct','mfe_pct','mae_pct','holding_min',
 'volume_z','taker_buy_ratio','flow_imbalance','open_interest','oi_change_1h',
 'funding_rate','basis_bps','btc_ret_1h','btc_volatility','relative_strength_1h','spread_bps'
]

def make_record(feature:dict, score:dict, state:dict) -> dict:
    f=normalize(feature)
    p=float(score['p_tp_before_sl']); ev=float(score['expected_net_pct'])
    risk=decide(p_tp=p,expected_net_pct=ev,spread_bps=f.spread_bps,
                stake_usdt=float(state.get('stake_usdt',10)),
                daily_pnl_usdt=float(state.get('daily_pnl_usdt',0)),
                open_positions=int(state.get('open_positions',0)),
                max_existing_corr=float(state.get('max_existing_corr',0)))
    decision='SHADOW_BUY' if risk['allow'] else 'SKIP'
    base={
      'ts':f.ts,'symbol':f.symbol,'setup_type':score.get('setup_type','MODEL'),
      'regime':score.get('regime','UNKNOWN'),'decision':decision,
      'reason':'OK' if risk['allow'] else '|'.join(risk['reasons']),
      'p_tp_before_sl':p,'expected_net_pct':ev,'stake_usdt':float(state.get('stake_usdt',10)),
      'entry_price':f.price,'tp_pct':float(score.get('tp_pct',1.2)),'sl_pct':float(score.get('sl_pct',0.7)),
      'status':'OPEN' if risk['allow'] else 'REJECTED','tp_before_sl':'','net_return_pct':'','mfe_pct':'','mae_pct':'','holding_min':'',
      'volume_z':f.volume_z,'taker_buy_ratio':f.taker_buy_ratio,'flow_imbalance':f.flow_imbalance,
      'open_interest':f.open_interest,'oi_change_1h':f.oi_change_1h,'funding_rate':f.funding_rate,
      'basis_bps':f.basis_bps,'btc_ret_1h':f.btc_ret_1h,'btc_volatility':f.btc_volatility,
      'relative_strength_1h':f.relative_strength_1h,'spread_bps':f.spread_bps,
    }
    return base

def append_record(rec:dict,path:pathlib.Path):
    path.parent.mkdir(parents=True,exist_ok=True); new=not path.exists()
    with path.open('a',newline='',encoding='utf-8') as f:
      w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore')
      if new:w.writeheader()
      w.writerow(rec)

def selftest():
    feature={"symbol":"SOLUSDT","ts":"2026-09-10T16:00:00Z","price":220.0,"ret_15m":.002,"ret_1h":.006,"volume_z":1.7,"taker_buy_ratio":.59,"flow_imbalance":.18,"open_interest":1000000.0,"oi_change_1h":.012,"funding_rate":.0001,"basis_bps":3.2,"btc_ret_1h":.003,"btc_volatility":.42,"relative_strength_1h":.003,"spread_bps":1.8}
    rec=make_record(feature,{"p_tp_before_sl":.68,"expected_net_pct":.45,"regime":"RISK_ON"},{"stake_usdt":10})
    assert rec['decision']=='SHADOW_BUY' and rec['status']=='OPEN'
    print(json.dumps({'selftest':'PASS','sample':rec}))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--selftest',action='store_true');a=ap.parse_args()
    if a.selftest:selftest()
if __name__=='__main__':main()
