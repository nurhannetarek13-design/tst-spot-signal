#!/usr/bin/env python3
"""Gate G shadow ablation layer. RESEARCH ONLY.
Control mirrors the base shadow decision. Treatment may VETO a base BUY using
L2 confirmation, but can NEVER create a BUY from a base SKIP.
"""
from __future__ import annotations
import argparse,csv,json,pathlib
from research.bybit_l2_live_feature import snapshot

FIELDS=['ts','symbol','base_decision','base_reason','control_decision','treatment_decision','treatment_reason','l2_exchange','obi10','microprice_bps','spread_bps','l2_long_confirm','p_tp_before_sl','expected_net_pct','entry_price','tp_pct','sl_pct']

def apply(base:dict,l2:dict|None=None,fetch_live:bool=True)->dict:
    decision=str(base.get('decision','SKIP'))
    out={'ts':base.get('ts',''),'symbol':base.get('symbol',''),'base_decision':decision,'base_reason':base.get('reason',''),
         'control_decision':decision,'treatment_decision':'SKIP','treatment_reason':'BASE_SKIP',
         'l2_exchange':'','obi10':'','microprice_bps':'','spread_bps':'','l2_long_confirm':'',
         'p_tp_before_sl':base.get('p_tp_before_sl',''),'expected_net_pct':base.get('expected_net_pct',''),
         'entry_price':base.get('entry_price',''),'tp_pct':base.get('tp_pct',''),'sl_pct':base.get('sl_pct','')}
    # Anti-action-bias invariant: treatment cannot promote a rejected base candidate.
    if decision!='SHADOW_BUY': return out
    x=l2 if l2 is not None else (snapshot(str(base['symbol'])) if fetch_live else None)
    if x is None: raise ValueError('missing_l2_for_base_buy')
    out.update({'l2_exchange':x.get('exchange','BYBIT'),'obi10':x['obi10'],'microprice_bps':x['microprice_bps'],'spread_bps':x['spread_bps'],'l2_long_confirm':bool(x['long_confirm'])})
    if bool(x['long_confirm']):
        out['treatment_decision']='SHADOW_BUY';out['treatment_reason']='L2_CONFIRM'
    else:
        out['treatment_decision']='SKIP';out['treatment_reason']='L2_VETO'
    return out

def append(row:dict,path:pathlib.Path):
    path.parent.mkdir(parents=True,exist_ok=True);new=not path.exists()
    with path.open('a',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore')
        if new:w.writeheader()
        w.writerow(row)

def selftest():
    skip={'ts':'2026-09-11T10:00:00Z','symbol':'SOLUSDT','decision':'SKIP','reason':'LOW_EDGE'}
    fake_yes={'exchange':'FIXTURE','obi10':.8,'microprice_bps':.2,'spread_bps':1.0,'long_confirm':True}
    a=apply(skip,fake_yes,False);assert a['treatment_decision']=='SKIP' and a['treatment_reason']=='BASE_SKIP'
    buy={'ts':'2026-09-11T10:00:00Z','symbol':'SOLUSDT','decision':'SHADOW_BUY','reason':'OK','p_tp_before_sl':.68,'expected_net_pct':.4,'entry_price':220,'tp_pct':1.2,'sl_pct':.7}
    b=apply(buy,fake_yes,False);assert b['control_decision']=='SHADOW_BUY' and b['treatment_decision']=='SHADOW_BUY'
    fake_no={**fake_yes,'obi10':.1,'microprice_bps':0.0,'long_confirm':False}
    c=apply(buy,fake_no,False);assert c['control_decision']=='SHADOW_BUY' and c['treatment_decision']=='SKIP' and c['treatment_reason']=='L2_VETO'
    print(json.dumps({'selftest':'PASS','base_skip_invariant':a,'confirm':b,'veto':c}))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--selftest',action='store_true');a=ap.parse_args()
    if a.selftest:selftest()
if __name__=='__main__':main()
