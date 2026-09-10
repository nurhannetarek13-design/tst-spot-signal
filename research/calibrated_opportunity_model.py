#!/usr/bin/env python3
"""Calibrated probability + expected-value research model for TST Spot.

RESEARCH ONLY. Chronological discovery -> calibration -> untouched test.
Shadow/forward labels are validation data unless a later experiment explicitly
freezes a training cohort. No random shuffle and no live execution imports.
"""
from __future__ import annotations
import argparse,json
from pathlib import Path
import numpy as np,pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression,Ridge
from sklearn.metrics import brier_score_loss,log_loss,mean_absolute_error,roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder,RobustScaler

META={'ts','symbol','setup_type','regime','tp_before_sl','net_return_pct','mfe_pct','mae_pct','holding_min','split','event_id','decision','reason'}

def _load(path:Path):
 df=pd.read_csv(path); missing={'ts','symbol','setup_type','regime','tp_before_sl','net_return_pct'}-set(df.columns)
 if missing:raise SystemExit(f'missing required columns: {sorted(missing)}')
 df=df.copy();df['ts']=pd.to_datetime(df['ts'],utc=True,errors='coerce');df=df.dropna(subset=['ts','symbol','tp_before_sl','net_return_pct']).sort_values('ts');df['tp_before_sl']=pd.to_numeric(df['tp_before_sl'],errors='coerce');df['net_return_pct']=pd.to_numeric(df['net_return_pct'],errors='coerce');df=df.dropna(subset=['tp_before_sl','net_return_pct']);df=df[df.tp_before_sl.isin([0,1])];return df.reset_index(drop=True)
def _features(df):
 cat=[c for c in ['setup_type','regime'] if c in df];num=[]
 for c in df:
  if c in META or c in cat:continue
  x=pd.to_numeric(df[c],errors='coerce')
  if x.notna().mean()>=.5 and x.nunique(dropna=True)>=3:num.append(c)
 if not num:raise SystemExit('no usable numeric features found')
 return num,cat
def _pre(num,cat):
 n=Pipeline([('impute',SimpleImputer(strategy='median')),('scale',RobustScaler())]);c=Pipeline([('impute',SimpleImputer(strategy='most_frequent')),('onehot',OneHotEncoder(handle_unknown='ignore'))]);return ColumnTransformer([('num',n,num),('cat',c,cat)])
def _split(df,discovery=.60,calibration=.20):
 n=len(df);a=max(1,int(n*discovery));b=max(a+1,int(n*(discovery+calibration)))
 if n-b<50:raise SystemExit('untouched test sample too small (<50 rows)')
 return df.iloc[:a],df.iloc[a:b],df.iloc[b:]
def _calibration_table(y,p,bins=10):
 out=[];edges=np.linspace(0,1,bins+1)
 for i in range(bins):
  lo,hi=edges[i],edges[i+1];m=(p>=lo)&((p<hi) if i<bins-1 else (p<=hi))
  if m.any():out.append({'bin_lo':round(float(lo),3),'bin_hi':round(float(hi),3),'n':int(m.sum()),'predicted':round(float(p[m].mean()),4),'observed':round(float(y[m].mean()),4)})
 return out
def _metrics(y,p,actual,pred):
 r={'n':int(len(y)),'base_rate':float(np.mean(y)),'brier':float(brier_score_loss(y,p)),'logloss':float(log_loss(y,p,labels=[0,1])),'net_mae_pct':float(mean_absolute_error(actual,pred)),'mean_actual_net_pct':float(np.mean(actual)),'mean_pred_net_pct':float(np.mean(pred))}
 try:r['auc']=float(roc_auc_score(y,p))
 except:r['auc']=None
 return r
def train(df,min_sample=300):
 if len(df)<min_sample:raise SystemExit(f'reject: sample {len(df)} < minimum {min_sample}')
 num,cat=_features(df);disc,cal,test=_split(df);cols=num+cat
 base=Pipeline([('pre',_pre(num,cat)),('model',LogisticRegression(C=.5,max_iter=3000,class_weight='balanced'))]);base.fit(disc[cols],disc.tp_before_sl.astype(int))
 # sklearn >=1.9 removed CalibratedClassifierCV(cv='prefit'). Fit isotonic explicitly
 # on the dedicated calibration slice, leaving the final test untouched.
 raw_cal=base.predict_proba(cal[cols])[:,1];iso=IsotonicRegression(out_of_bounds='clip',y_min=1e-6,y_max=1-1e-6);iso.fit(raw_cal,cal.tp_before_sl.astype(int).to_numpy())
 evm=Pipeline([('pre',_pre(num,cat)),('model',Ridge(alpha=10.0))]);train_ev=pd.concat([disc,cal]);evm.fit(train_ev[cols],train_ev.net_return_pct)
 raw_test=base.predict_proba(test[cols])[:,1];p=np.clip(iso.transform(raw_test),1e-6,1-1e-6);ev=evm.predict(test[cols]);y=test.tp_before_sl.astype(int).to_numpy();actual=test.net_return_pct.to_numpy(float)
 report={'status':'RESEARCH_ONLY','liveTrading':False,'model_family':'logistic+explicit_isotonic_probability / ridge_net_EV','rows_total':int(len(df)),'rows_discovery':len(disc),'rows_calibration':len(cal),'rows_untouched_test':len(test),'numeric_features':num,'categorical_features':cat,'untouched_test':_metrics(y,p,actual,ev),'calibration_bins':_calibration_table(y,p)}
 scored=test[['ts','symbol','setup_type','regime','tp_before_sl','net_return_pct']].copy();scored['p_tp_before_sl']=p;scored['expected_net_pct']=ev
 symbols=sorted(df.symbol.astype(str).unique());cut=max(1,int(len(symbols)*.8));held=set(symbols[cut:]);hold=test[test.symbol.astype(str).isin(held)]
 if len(hold)>=30:
  hp=np.clip(iso.transform(base.predict_proba(hold[cols])[:,1]),1e-6,1-1e-6);hev=evm.predict(hold[cols]);report['symbol_holdout_test']=_metrics(hold.tp_before_sl.astype(int).to_numpy(),hp,hold.net_return_pct.to_numpy(float),hev);report['symbol_holdout_symbols']=sorted(held)
 else:report['symbol_holdout_test']={'status':'INSUFFICIENT_SAMPLE','n':int(len(hold))}
 m=report['untouched_test'];auc_ok=m.get('auc') is not None and m['auc']>=.56;cal_ok=m['brier']<(m['base_rate']*(1-m['base_rate']));ev_ok=m['net_mae_pct']<=max(.75,float(np.std(actual)))
 report['discovery_gate']={'pass':bool(auc_ok and cal_ok and ev_ok),'auc_ge_0_56':bool(auc_ok),'brier_better_than_base':bool(cal_ok),'ev_error_sane':bool(ev_ok),'liveAuthorized':False,'note':'PASS means proceed to walk-forward/cost stress; never direct-live approval.'};return report,scored
def selftest():
 rng=np.random.default_rng(7);n=500;t=pd.date_range('2025-01-01',periods=n,freq='h',tz='UTC');x=rng.normal(size=n);p=1/(1+np.exp(-(.7*x)));y=(rng.random(n)<p).astype(int);net=np.where(y==1,.9,-.7)+rng.normal(0,.1,n);df=pd.DataFrame({'ts':t,'symbol':np.where(np.arange(n)%2,'ETHUSDT','SOLUSDT'),'setup_type':'TEST','regime':'TEST','tp_before_sl':y,'net_return_pct':net,'feature_x':x});r,_=train(df,300);assert r['rows_untouched_test']>=50 and r['liveTrading'] is False;print(json.dumps({'selftest':'PASS','test':r['untouched_test'],'gate':r['discovery_gate']}))
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--input',type=Path);ap.add_argument('--report',type=Path);ap.add_argument('--scores',type=Path);ap.add_argument('--min-sample',type=int,default=300);ap.add_argument('--selftest',action='store_true');a=ap.parse_args()
 if a.selftest:return selftest()
 if not a.input or not a.report:raise SystemExit('--input and --report required')
 r,s=train(_load(a.input),a.min_sample);a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(r,indent=2,default=str));
 if a.scores:a.scores.parent.mkdir(parents=True,exist_ok=True);s.to_csv(a.scores,index=False)
 print(json.dumps(r['discovery_gate'],indent=2))
if __name__=='__main__':main()
