#!/usr/bin/env python3
from __future__ import annotations
import json,math,pathlib
IN=pathlib.Path('validation/pro_stack/feature_store.json');OUT=pathlib.Path('validation/pro_stack/edge_scores.json')
def sigmoid(x):return 1/(1+math.exp(-x))
def score(r):
 z=0.0;z+=0.7*r['flowZ'];z+=0.45*r['quoteVolumeZ'];z+=8.0*r['ret1h'];z+=4.0*r['relativeStrength1hVsBTC'];z+=3.0*r['btcRet1h'];z-=2.5*r['realizedVol1h']
 p=sigmoid(z); gross=(p-.5)*0.03; costs=.0028; net=gross-costs
 return {'symbol':r['symbol'],'pTpBeforeSl':p,'expectedGrossReturn':gross,'expectedCosts':costs,'expectedNetEdge':net,'decision':'SHADOW_BUY' if p>=.65 and net>0 else 'SKIP'}
def main():
 x=json.load(open(IN));rows=[score(r) for r in x['rows'] if r['symbol']!='BTCUSDT'];o={'engine':'PRO_STACK_EDGE_MODEL_V1','authorization':'RESEARCH_ONLY','liveTrading':False,'modelType':'frozen logistic-style scoring baseline','rows':rows};OUT.write_text(json.dumps(o,indent=2));print(json.dumps(o))
if __name__=='__main__':main()
