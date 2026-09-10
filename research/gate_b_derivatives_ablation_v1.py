#!/usr/bin/env python3
"""Gate B: derivatives feature-value ablation, research only.

Purpose: test whether Binance USD-M derivatives features add incremental OOS value
relative to a price/flow baseline on the SAME rows and SAME forward labels.
This is NOT Gate A and never mutates Gate A state.

Chronological split: 60% discovery / 20% calibration / 20% untouched test.
Label: +1.2% TP before -0.7% SL within 4h (15m bars), conservative same-bar SL wins.
Net return uses 0.28% round-trip research cost.
"""
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss,log_loss,roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler
from research.binance_vision_historical_feature_layer import build

AUTH='RESEARCH_ONLY'; TP=.012; SL=.007; H=16; COST=.0028
BASE_FEATURES=['ret_1bar','flow_imbalance_quote','buy_share_quote','taker_buy_sell_ratio_quote','realized_range_bps','agg_trade_count','avg_trade_quote']
DERIV_FEATURES=['fut_sum_open_interest','fut_sum_open_interest_value','fut_count_toptrader_long_short_ratio','fut_sum_toptrader_long_short_ratio','fut_count_long_short_ratio','fut_sum_taker_long_short_vol_ratio','fut_oi_change_1bar','fut_oi_value_change_1bar','mark_index_basis_bps']

def add_features(d):
 d=d.sort_values('ts').copy()
 for c in BASE_FEATURES+DERIV_FEATURES:
  if c in d:
   d[c]=pd.to_numeric(d[c],errors='coerce')
 # lag every predictive feature by one bar to avoid using information from the label bar close.
 for c in BASE_FEATURES+DERIV_FEATURES:
  if c in d:d[c]=d[c].shift(1)
 # stabilize levels into changes/z-scores where possible
 for c in ['fut_sum_open_interest','fut_sum_open_interest_value','agg_trade_count','avg_trade_quote']:
  if c in d:d[c+'_z96']=(d[c]-d[c].rolling(96).mean())/d[c].rolling(96).std()
 d['flow_z96']=(d['flow_imbalance_quote']-d['flow_imbalance_quote'].rolling(96).mean())/d['flow_imbalance_quote'].rolling(96).std()
 d['basis_z96']=(d.get('mark_index_basis_bps')-d.get('mark_index_basis_bps').rolling(96).mean())/d.get('mark_index_basis_bps').rolling(96).std() if 'mark_index_basis_bps' in d else np.nan
 return d

def label(d):
 hi=pd.to_numeric(d['trade_high'],errors='coerce').to_numpy(float); lo=pd.to_numeric(d['trade_low'],errors='coerce').to_numpy(float); close=pd.to_numeric(d['trade_close'],errors='coerce').to_numpy(float)
 y=np.full(len(d),np.nan); net=np.full(len(d),np.nan)
 for i in range(len(d)-H-1):
  e=close[i]; outcome=None; px=None
  for j in range(i+1,min(len(d),i+H+1)):
   if lo[j] <= e*(1-SL): outcome=0; px=e*(1-SL); break
   if hi[j] >= e*(1+TP): outcome=1; px=e*(1+TP); break
  if outcome is None:
   outcome=int(close[i+H]>e); px=close[i+H]
  y[i]=outcome; net[i]=(px/e-1)-COST
 d=d.copy();d['tp_before_sl']=y;d['net_return']=net;return d

def model_report(df,features):
 x=df.dropna(subset=['tp_before_sl']).copy(); n=len(x); a=int(n*.6); b=int(n*.8)
 if n<500 or n-b<100:return {'status':'INSUFFICIENT','n':n}
 disc,cal,test=x.iloc[:a],x.iloc[a:b],x.iloc[b:]
 pipe=Pipeline([('imp',SimpleImputer(strategy='median')),('scale',RobustScaler()),('m',LogisticRegression(C=.5,max_iter=3000,class_weight='balanced'))])
 pipe.fit(disc[features],disc.tp_before_sl.astype(int)); clf=CalibratedClassifierCV(pipe,method='isotonic',cv='prefit');clf.fit(cal[features],cal.tp_before_sl.astype(int))
 p=clf.predict_proba(test[features])[:,1]; y=test.tp_before_sl.astype(int).to_numpy(); base=float(y.mean()); brier=float(brier_score_loss(y,p));
 try:auc=float(roc_auc_score(y,p))
 except:auc=None
 # Decision thresholds are frozen before viewing test: 0.60/0.65/0.70.
 th={}
 for t in (.60,.65,.70):
  m=p>=t; vals=test.net_return.to_numpy(float)[m]
  wins=vals[vals>0].sum() if len(vals) else 0; losses=-vals[vals<0].sum() if len(vals) else 0
  th[str(t)]={'n':int(m.sum()),'meanNetPct':float(vals.mean()*100) if len(vals) else None,'hitRate':float((vals>0).mean()) if len(vals) else None,'profitFactor':float(wins/losses) if losses>0 else (99. if wins>0 else 0.)}
 return {'status':'OK','n':n,'discovery':len(disc),'calibration':len(cal),'test':len(test),'auc':auc,'brier':brier,'baseRate':base,'brierBaseline':base*(1-base),'logloss':float(log_loss(y,p,labels=[0,1])),'thresholds':th}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--days',type=int,default=30);ap.add_argument('--symbols',default='ETHUSDT,SOLUSDT');ap.add_argument('--out',default='validation/edges/gate-b-derivatives-ablation-v1.json');a=ap.parse_args()
 end=(datetime.now(timezone.utc)-timedelta(days=2)).date(); start=end-timedelta(days=a.days-1); frames=[]; meta=[]
 for s in [z.strip().upper() for z in a.symbols.split(',') if z.strip()]:
  d,m=build(s,start,end,'15min',True); d=label(add_features(d)); frames.append(d);meta.append(m)
 all=pd.concat(frames,ignore_index=True).sort_values('ts').reset_index(drop=True)
 base=BASE_FEATURES+['flow_z96','agg_trade_count_z96','avg_trade_quote_z96']
 ext=base+DERIV_FEATURES+['fut_sum_open_interest_z96','fut_sum_open_interest_value_z96','basis_z96']
 base=[c for c in base if c in all];ext=[c for c in ext if c in all]
 rb=model_report(all,base);re=model_report(all,ext)
 improvement={'aucDelta':None if rb.get('auc') is None or re.get('auc') is None else re['auc']-rb['auc'],'brierDelta':None if rb.get('brier') is None else re['brier']-rb['brier']}
 # Research pass requires better AUC by >=.015, lower Brier, and a positive PF>=1.25 at >=0.65 with >=30 test trades.
 t=re.get('thresholds',{}).get('0.65',{}); passed=bool(improvement['aucDelta'] is not None and improvement['aucDelta']>=.015 and improvement['brierDelta'] is not None and improvement['brierDelta']<0 and t.get('n',0)>=30 and (t.get('profitFactor') or 0)>=1.25 and (t.get('meanNetPct') or -999)>0)
 out={'engine':'GATE_B_DERIVATIVES_ABLATION_V1','authorization':AUTH,'liveTrading':False,'mutatesGateA':False,'featureAddition':'BINANCE_USDM_DERIVATIVES','label':{'tpPct':1.2,'slPct':0.7,'horizonMin':240,'sameBarRule':'SL_WINS','roundTripCostPct':0.28},'period':{'start':start.isoformat(),'end':end.isoformat()},'symbols':[m['symbol'] for m in meta],'rows':len(all),'baseFeatures':base,'extendedFeatures':ext,'base':rb,'extended':re,'improvement':improvement,'researchGatePass':passed,'next':'FORWARD_GATE_B_SHADOW_AFTER_GATE_A_FREEZE' if passed else 'REJECT_DERIVATIVES_FEATURE_SET_V1'}
 p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2,default=str));print(json.dumps({'rows':out['rows'],'improvement':improvement,'researchGatePass':passed,'next':out['next']}))
if __name__=='__main__':main()
