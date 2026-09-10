#!/usr/bin/env python3
from __future__ import annotations
import json,time,urllib.parse,urllib.request,pathlib
import numpy as np,pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss,roc_auc_score
BASE='https://data-api.binance.vision';OUT=pathlib.Path('validation/pro_stack/model.json');SYMS=['ETHUSDT','SOLUSDT'];BTC='BTCUSDT';TP=.012;SL=.007;H=16;COST=.0028
def api(p):
 r=urllib.request.Request(BASE+p,headers={'User-Agent':'tst-pro-ml/1.0'});return json.load(urllib.request.urlopen(r,timeout=30))
def load(sym,limit=1000):
 q=urllib.parse.urlencode({'symbol':sym,'interval':'15m','limit':limit});b=api('/api/v3/klines?'+q);c=['ot','open','high','low','close','volume','ct','qv','trades','tb','tq','ignore'];d=pd.DataFrame(b,columns=c)
 for x in ['open','high','low','close','volume','qv','tq']:d[x]=pd.to_numeric(d[x],errors='coerce')
 return d
def features(d,btc):
 f=pd.DataFrame(index=d.index);r1=d.close.pct_change();f['ret15m']=r1;f['ret1h']=d.close.pct_change(4);f['ret4h']=d.close.pct_change(16);f['qvz']=(d.qv-d.qv.rolling(96).mean())/d.qv.rolling(96).std();flow=d.tq/d.qv.replace(0,np.nan);f['flowz']=(flow-flow.rolling(96).mean())/flow.rolling(96).std();f['buyshare']=flow;f['rv1h']=r1.rolling(16).std()*np.sqrt(16);br=btc.close.pct_change(4);f['btc1h']=br;f['rel1h']=f.ret1h-br;return f
def label(d,i):
 e=float(d.close.iloc[i]); end=min(len(d),i+H+1)
 for j in range(i+1,end):
  if float(d.low.iloc[j])<=e*(1-SL): return 0
  if float(d.high.iloc[j])>=e*(1+TP): return 1
 return np.nan
def main():
 btc=load(BTC);xs=[];ys=[];meta=[]
 for s in SYMS:
  d=load(s);f=features(d,btc); y=pd.Series([label(d,i) for i in range(len(d))],index=d.index);z=f.copy();z['y']=y;z=z.dropna();xs.append(z.drop(columns='y'));ys.append(z.y.astype(int));meta += [s]*len(z)
 X=pd.concat(xs,ignore_index=True);Y=pd.concat(ys,ignore_index=True);n=len(X);a=int(n*.6);b=int(n*.8);cols=list(X.columns)
 mu=X.iloc[:a].mean();sd=X.iloc[:a].std().replace(0,1);Z=(X-mu)/sd
 m=LogisticRegression(C=0.5,max_iter=2000,class_weight='balanced',random_state=7).fit(Z.iloc[:a],Y.iloc[:a])
 def eval(lo,hi):
  p=m.predict_proba(Z.iloc[lo:hi])[:,1];y=Y.iloc[lo:hi].to_numpy();mask=p>=.65
  auc=float(roc_auc_score(y,p)) if len(np.unique(y))>1 else .5;brier=float(brier_score_loss(y,p));hit=float(y[mask].mean()) if mask.any() else 0.;nsel=int(mask.sum());exp=hit*TP-(1-hit)*SL-COST if nsel else -1
  return {'n':len(y),'auc':auc,'brier':brier,'selected':nsel,'selectedHitRate':hit,'expectedNetPerSelected':exp}
 val=eval(a,b);test=eval(b,n);passed=val['selected']>=20 and test['selected']>=20 and val['auc']>=.52 and test['auc']>=.52 and val['expectedNetPerSelected']>0 and test['expectedNetPerSelected']>0
 o={'engine':'PRO_STACK_ML_EDGE_MODEL_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'features':cols,'mean':{k:float(v) for k,v in mu.items()},'std':{k:float(v) for k,v in sd.items()},'coef':[float(x) for x in m.coef_[0]],'intercept':float(m.intercept_[0]),'threshold':.65,'label':{'tp':TP,'sl':SL,'horizonBars':H,'cost':COST},'validation':val,'test':test,'qualityGatePassed':bool(passed)};OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(o,indent=2));print(json.dumps({'qualityGatePassed':passed,'validation':val,'test':test}))
if __name__=='__main__':main()
