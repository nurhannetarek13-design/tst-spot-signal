from __future__ import annotations

import hashlib,json,os,statistics,time
from pathlib import Path
import shadow_ev_model as m

APPROVAL_PATH=Path(os.getenv('TST_EV_LIVE_APPROVAL_PATH','/data/tst_ev_live_approval.json'))
REPORT_PATH=Path(os.getenv('TST_SHADOW_EV_REPORT_PATH','/data/tst_shadow_ev_report.json'))
MIN_FORWARD=max(400,int(os.getenv('EV_PROMOTION_MIN_FORWARD_SAMPLE','600')))
MIN_FOLD_TEST=max(40,int(os.getenv('EV_PROMOTION_MIN_FOLD_TEST','60')))
FOLDS=max(3,min(6,int(os.getenv('EV_PROMOTION_FOLDS','4'))))
EXTRA_STRESS_COST_PCT=max(0.0,float(os.getenv('EV_PROMOTION_EXTRA_STRESS_COST_PCT','0.28')))
POLL_SEC=max(300,int(os.getenv('EV_PROMOTION_POLL_SEC','900')))

def _atomic(row):
    APPROVAL_PATH.parent.mkdir(parents=True,exist_ok=True); t=APPROVAL_PATH.with_suffix('.tmp'); t.write_text(json.dumps(row,separators=(',',':')),encoding='utf-8'); os.replace(t,APPROVAL_PATH)

def _report():
    try:
        x=json.loads(REPORT_PATH.read_text(encoding='utf-8')); return x if isinstance(x,dict) else {}
    except Exception:return {}

def _fit_predict(train,test):
    if len(train)<120 or len(test)<20:return [],[]
    cut=max(80,int(len(train)*.8)); fit=train[:cut]; cal=train[cut:]
    schema=m._fit_schema(fit); xf=[m._vec(r,schema) for r in fit]; xt=[m._vec(r,schema) for r in test]
    y=[int(r['tp_before_sl']) for r in fit]
    if len(set(y))<2 or min(sum(y),len(y)-sum(y))<25:return [],[]
    pw=m._fit_logistic(xf,y); platt=[0.0,1.0]
    if len(cal)>=30:
        xc=[m._vec(r,schema) for r in cal]; cy=[int(r['tp_before_sl']) for r in cal]
        if len(set(cy))==2: platt=m._fit_platt([m._sigmoid(m._dot(pw,x)) for x in xc],cy)
    probs=[m._calibrate(m._sigmoid(m._dot(pw,x)),platt) for x in xt]
    # EV regression may use full pre-test history; test remains untouched.
    schema2=m._fit_schema(train); xall=[m._vec(r,schema2) for r in train]; xte=[m._vec(r,schema2) for r in test]
    nw=m._fit_linear(xall,[float(r['sim_net_pct']) for r in train]); evs=[m._dot(nw,x) for x in xte]
    return probs,evs

def _metrics(test,probs,evs):
    if not test or not probs or not evs:return {'n':len(test),'pass':False}
    k=max(10,int(len(test)*.2)); ids=sorted(range(len(test)),key=lambda i:evs[i],reverse=True)[:k]
    vals=[float(test[i]['sim_net_pct'])-EXTRA_STRESS_COST_PCT for i in ids]; pp=[probs[i] for i in ids]; yy=[int(test[i]['tp_before_sl']) for i in ids]
    mean=sum(vals)/len(vals); med=statistics.median(vals); hit=sum(v>0 for v in vals)/len(vals); gap=abs(sum(pp)/len(pp)-sum(yy)/len(yy)); ci=m._bootstrap_mean_ci(vals); lo=ci[0]
    passed=mean>0 and med>-.05 and hit>=.50 and gap<=.12 and lo is not None and lo>-.10
    return {'n':len(test),'top_n':len(vals),'mean_stressed_net_pct':round(mean,4),'median_stressed_net_pct':round(med,4),'hit_rate_stressed':round(hit,4),'probability_calibration_gap_top':round(gap,4),'bootstrap95':ci,'pass':passed}

def _symbol_holdout(rows):
    hold=[r for r in rows if int(hashlib.sha256(str(r.get('symbol') or '').encode()).hexdigest()[:8],16)%5==0]
    train=[r for r in rows if int(hashlib.sha256(str(r.get('symbol') or '').encode()).hexdigest()[:8],16)%5!=0]
    syms=len(set(str(r.get('symbol')) for r in hold))
    if len(hold)<80 or syms<5:return {'n':len(hold),'symbols':syms,'pass':False,'reason':'insufficient-unseen-symbols'}
    p,e=_fit_predict(train,hold); out=_metrics(hold,p,e); out['symbols']=syms; return out

def build():
    rows=[r for r in m._rows() if r.get('tp_before_sl') in {0,1} and r.get('sim_net_pct') is not None]
    rep=_report(); base={'version':2,'feature_version':m.FEATURE_VERSION,'generated_at':time.time(),'minimum_forward_sample':MIN_FORWARD,'forward_sample':len(rows),'extra_stress_cost_pct':EXTRA_STRESS_COST_PCT,'automatic_order_execution':False}
    if len(rows)<MIN_FORWARD:return {**base,'status':'WAITING_FORWARD_SAMPLE','approved_at':None,'folds':[]}
    if not bool(rep.get('evidence_pass')):return {**base,'status':'WAITING_BASE_MODEL_EVIDENCE','approved_at':None,'folds':[]}
    n=len(rows); initial=max(240,int(n*.4)); step=max(MIN_FOLD_TEST,(n-initial)//FOLDS); folds=[]; cur=initial
    while cur<n and len(folds)<FOLDS:
        end=n if len(folds)==FOLDS-1 else min(n,cur+step); test=rows[cur:end]
        if len(test)<MIN_FOLD_TEST:break
        p,e=_fit_predict(rows[:cur],test); fm=_metrics(test,p,e); fm.update({'train_n':cur,'start_ts':test[0].get('source_ts') or test[0].get('ts'),'end_ts':test[-1].get('source_ts') or test[-1].get('ts')}); folds.append(fm); cur=end
    fold_pass=len(folds)>=3 and all(x.get('pass') for x in folds)
    hold=_symbol_holdout(rows)
    prob=rep.get('probability_test') or {}; cal_ok=float(prob.get('brier_skill') or -1)>.02 and float(prob.get('calibration_mae') or 99)<=.10
    regime_counts=rep.get('test_regime_counts') or {}; regime_ok=sum(1 for v in regime_counts.values() if int(v)>=15)>=3
    approved=fold_pass and bool(hold.get('pass')) and cal_ok and regime_ok
    return {**base,'status':'APPROVED' if approved else 'NOT_APPROVED','approved_at':time.time() if approved else None,'folds':folds,'fold_pass':fold_pass,'true_unseen_symbol_holdout':hold,'symbol_holdout_pass':bool(hold.get('pass')),'calibration_pass':cal_ok,'regime_coverage_pass':regime_ok,'base_evidence_pass':bool(rep.get('evidence_pass'))}

def run_once():
    r=build(); _atomic(r); print(f"[ev-validation] status={r['status']} n={r['forward_sample']} folds={len(r.get('folds') or [])} holdout={r.get('symbol_holdout_pass')} feature={r['feature_version']}",flush=True); return r

def main():
    print(f'[ev-validation] ONLINE leakage_safe=True true_symbol_holdout=True min_forward={MIN_FORWARD} folds={FOLDS} extra_stress={EXTRA_STRESS_COST_PCT:.2f}%',flush=True)
    while True:
        t=time.time()
        try:run_once()
        except Exception as exc:print(f'[ev-validation] warning {type(exc).__name__}: {str(exc)[:180]}',flush=True)
        time.sleep(max(60,POLL_SEC-(time.time()-t)))
if __name__=='__main__': run_once() if '--once' in __import__('sys').argv else main()
