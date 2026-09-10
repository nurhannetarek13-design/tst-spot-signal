#!/usr/bin/env python3
"""Gate C nonlinear interaction model, research only.

This is a NEW experiment after Gate B V1 was consumed/rejected. It does not tune
thresholds on Gate B's test. Uses economically-motivated stationary transforms
(flow, OI change, basis z, relative momentum, volatility) and nonlinear tree
interactions. Evaluation is chronological walk-forward with fixed decision rule.
No live execution and no Gate A state mutation.
"""
from __future__ import annotations
import argparse,json,pathlib
from datetime import datetime,timedelta,timezone
import numpy as np,pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss,roc_auc_score
from sklearn.pipeline import Pipeline
from research.binance_vision_historical_feature_layer import build

AUTH='RESEARCH_ONLY';TP=.012;SL=.007;H=16;COST=.0028;TH=.60
BASECOLS=['ret_1bar','flow_imbalance_quote','buy_share_quote','taker_buy_sell_ratio_quote','realized_range_bps','agg_trade_count','avg_trade_quote']
DERIV=['fut_oi_change_1bar','fut_oi_value_change_1bar','fut_sum_taker_long_short_vol_ratio','fut_sum_toptrader_long_short_ratio','fut_count_long_short_ratio','mark_index_basis_bps']

def prep(d):
 d=d.sort_values('ts').copy()
 for c in BASECOLS+DERIV:
  if c in d:d[c]=pd.to_numeric(d[c],errors='coerce')
 # all predictive inputs lagged one completed bar
 for c in BASECOLS+DERIV:
  if c in d:d[c]=d[c].shift(1)
 def z(c,w=96):
  x=d[c];return (x-x.rolling(w).mean())/x.rolling(w).std()
 d['flow_z']=z('flow_imbalance_quote');d['vol_z']=z('agg_trade_count');d['range_z']=z('realized_range_bps')
 d['basis_z']=z('mark_index_basis_bps') if 'mark_index_basis_bps' in d else np.nan
 d['taker_ls_z']=z('fut_sum_taker_long_short_vol_ratio') if 'fut_sum_taker_long_short_vol_ratio' in d else np.nan
 d['top_ls_z']=z('fut_sum_toptrader_long_short_ratio') if 'fut_sum_toptrader_long_short_ratio' in d else np.nan
 # fixed, interpretable interactions frozen before evaluation
 d['flow_x_oi']=d['flow_z']*d['fut_oi_change_1bar']
 d['flow_x_basis']=d['flow_z']*d['basis_z']
 d['oi_x_basis']=d['fut_oi_change_1bar']*d['basis_z']
 d['flow_x_takerls']=d['flow_z']*d['taker_ls_z']
 return d

def label(d):
 hi=pd.to_numeric(d.trade_high,errors='coerce').to_numpy(float);lo=pd.to_numeric(d.trade_low,errors='coerce').to_numpy(float);cl=pd.to_numeric(d.trade_close,errors='coerce').to_numpy(float)
 y=np.full(len(d),np.nan);net=np.full(len(d),np.nan)
 for i in range(len(d)-H-1):
  e=cl[i];o=None;px=None
  for j in range(i+1,i+H+1):
   if lo[j]<=e*(1-SL):o=0;px=e*(1-SL);break
   if hi[j]>=e*(1+TP):o=1;px=e*(1+TP);break
  if o is None:o=int(cl[i+H]>e);px=cl[i+H]
  y[i]=o;net[i]=px/e-1-COST
 d=d.copy();d['y']=y;d['net']=net;return d

def pf(vals):
 vals=np.asarray(vals,float);w=vals[vals>0].sum();l=-vals[vals<0].sum();return float(w/l) if l>0 else (99. if w>0 else 0.)
def fold(train,cal,test,features):
 model=Pipeline([('imp',SimpleImputer(strategy='median')),('m',HistGradientBoostingClassifier(max_iter=120,learning_rate=.04,max_leaf_nodes=15,l2_regularization=2.0,min_samples_leaf=30,random_state=17))])
 model.fit(train[features],train.y.astype(int));pc=model.predict_proba(cal[features])[:,1];iso=IsotonicRegression(out_of_bounds='clip',y_min=1e-6,y_max=1-1e-6).fit(pc,cal.y.astype(int).to_numpy());p=np.clip(iso.transform(model.predict_proba(test[features])[:,1]),1e-6,1-1e-6);y=test.y.astype(int).to_numpy();m=p>=TH;v=test.net.to_numpy(float)[m]
 try:auc=float(roc_auc_score(y,p))
 except:auc=None
 base=float(y.mean());return {'nTest':len(test),'auc':auc,'brier':float(brier_score_loss(y,p)),'brierBaseline':base*(1-base),'selected':int(m.sum()),'selectedRate':float(m.mean()),'meanNetPct':float(v.mean()*100) if len(v) else None,'hitRate':float((v>0).mean()) if len(v) else None,'profitFactor':pf(v) if len(v) else 0.0}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--days',type=int,default=60);ap.add_argument('--out',default='validation/edges/gate-c-interaction-model-v1.json');a=ap.parse_args();end=(datetime.now(timezone.utc)-timedelta(days=2)).date();start=end-timedelta(days=a.days-1);fs=[]
 for s in ['ETHUSDT','SOLUSDT']:
  d,_=build(s,start,end,'15min',True);d['symbol']=s;fs.append(label(prep(d)))
 x=pd.concat(fs,ignore_index=True).sort_values('ts').dropna(subset=['y']).reset_index(drop=True)
 features=['ret_1bar','flow_z','vol_z','range_z','buy_share_quote','taker_buy_sell_ratio_quote','fut_oi_change_1bar','fut_oi_value_change_1bar','basis_z','taker_ls_z','top_ls_z','flow_x_oi','flow_x_basis','oi_x_basis','flow_x_takerls'];features=[c for c in features if c in x]
 # 3 expanding chronological folds. Each has train/cal/test in strict time order.
 n=len(x);cuts=[(.40,.50,.625),(.50,.625,.75),(.625,.75,1.0)];folds=[]
 for tr,ca,te in cuts:
  ia=int(n*tr);ib=int(n*ca);ic=int(n*te);train=x.iloc[:ia];cal=x.iloc[ia:ib];test=x.iloc[ib:ic]
  if len(train)>=500 and len(cal)>=100 and len(test)>=100:folds.append(fold(train,cal,test,features))
 good=[f for f in folds if f['auc'] is not None];total_sel=sum(f['selected'] for f in good);weighted_mean=sum((f['meanNetPct'] or 0)*f['selected'] for f in good)/total_sel if total_sel else None;all_pf_ok=all(f['profitFactor']>=1.20 for f in good if f['selected']>=20);all_brier=all(f['brier']<f['brierBaseline'] for f in good);auc_med=float(np.median([f['auc'] for f in good])) if good else None
 passed=bool(len(good)>=3 and total_sel>=60 and auc_med>=.56 and all_brier and all_pf_ok and weighted_mean is not None and weighted_mean>0)
 out={'engine':'GATE_C_INTERACTION_MODEL_V1','authorization':AUTH,'liveTrading':False,'mutatesGateA':False,'gateBTestReusedForTuning':False,'period':{'start':start.isoformat(),'end':end.isoformat()},'rows':len(x),'features':features,'model':'HistGradientBoosting + separate isotonic calibration','fixedDecisionThreshold':TH,'costRoundTripPct':.28,'folds':folds,'summary':{'medianAUC':auc_med,'totalSelected':total_sel,'weightedMeanNetPct':weighted_mean,'allBrierBetterThanBase':all_brier,'allEligibleFoldPFGe1_20':all_pf_ok},'researchGatePass':passed,'next':'FORWARD_SHADOW_CANDIDATE_REVIEW' if passed else 'REJECT_GATE_C_V1'};p=pathlib.Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2));print(json.dumps({'summary':out['summary'],'researchGatePass':passed,'next':out['next']}))
if __name__=='__main__':main()
