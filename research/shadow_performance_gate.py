#!/usr/bin/env python3
"""Research-only performance gate for labeled Spot-baseline shadow trades."""
from __future__ import annotations
import argparse,csv,json,pathlib

def metrics(rows):
    c=[r for r in rows if r.get('status')=='CLOSED' and str(r.get('net_return_pct','')).strip()!='']
    vals=[float(r['net_return_pct']) for r in c]
    wins=sum(v for v in vals if v>0); losses=-sum(v for v in vals if v<0)
    pf=(wins/losses) if losses>0 else (99.0 if wins>0 else 0.0)
    hit=(sum(v>0 for v in vals)/len(vals)) if vals else 0.0
    mean=(sum(vals)/len(vals)) if vals else 0.0
    maxdd=0.0; eq=0.0; peak=0.0
    for v in vals:
        eq+=v; peak=max(peak,eq); maxdd=max(maxdd,peak-eq)
    return {'n':len(vals),'profitFactor':pf,'hitRate':hit,'meanNetPct':mean,'maxDrawdownPct':maxdd}

def milestone(n):
    if n<10:return {'stage':'COLLECTING_LT_10','diagnosticOnly':True,'nextAt':10}
    if n<25:return {'stage':'DIAGNOSTIC_10','diagnosticOnly':True,'nextAt':25}
    if n<50:return {'stage':'DIAGNOSTIC_25','diagnosticOnly':True,'nextAt':50}
    return {'stage':'DECISION_50_PLUS','diagnosticOnly':False,'nextAt':None}

def gate(m):
    reasons=[]
    if m['n']<50:reasons.append('SAMPLE_LT_50')
    if m['profitFactor']<1.25:reasons.append('PF_LT_1_25')
    if m['meanNetPct']<=0:reasons.append('EXPECTANCY_NONPOSITIVE')
    if m['hitRate']<0.55:reasons.append('HIT_LT_55')
    if m['maxDrawdownPct']>8.0:reasons.append('DD_GT_8PCT')
    return {'pass':not reasons,'reasons':reasons,'liveAuthorized':False,'retrainingAuthorized':False,'next':'FREEZE_GATE_A_RESULT_AND_OPEN_GATE_B_RESEARCH' if not reasons else 'KEEP_GATE_A_SHADOW'}

def selftest():
    rows=[{'status':'CLOSED','net_return_pct':'0.8'} for _ in range(35)]+[{'status':'CLOSED','net_return_pct':'-0.5'} for _ in range(15)]
    m=metrics(rows); g=gate(m); assert m['n']==50 and milestone(25)['diagnosticOnly'] and not milestone(50)['diagnosticOnly'] and g['liveAuthorized'] is False and g['retrainingAuthorized'] is False
    print(json.dumps({'selftest':'PASS','metrics':m,'milestone':milestone(m['n']),'gate':g}))
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',type=pathlib.Path);ap.add_argument('--output',type=pathlib.Path);ap.add_argument('--selftest',action='store_true');a=ap.parse_args()
    if a.selftest:return selftest()
    rows=list(csv.DictReader(a.input.open(encoding='utf-8')));o={'engine':'SHADOW_PERFORMANCE_GATE_A_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'shadowLabelsRole':'PURE_VALIDATION','metrics':metrics(rows)};o['milestone']=milestone(o['metrics']['n']);o['gate']=gate(o['metrics']);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(o,indent=2));print(json.dumps(o))
if __name__=='__main__':main()
