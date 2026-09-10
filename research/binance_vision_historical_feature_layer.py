#!/usr/bin/env python3
"""Research-only historical Binance USD-M feature layer.

Sources (Binance Public Data / data.binance.vision):
- aggTrades: aggressive buy/sell flow, delta, CVD, trade intensity
- markPriceKlines / indexPriceKlines / premiumIndexKlines
- metrics: open interest, trader positioning, global long/short, taker long/short

Order-book features are intentionally excluded: aggTrades/metrics are not L2.
No account/order endpoints, API keys, or live trading.
"""
from __future__ import annotations
import argparse, hashlib, io, json, pathlib, urllib.error, urllib.request, zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd

AUTHORIZATION="RESEARCH_ONLY"
BASE="https://data.binance.vision/data/futures/um"
UA="tst-binance-vision-historical-feature-layer/1.2"
AGG_COLS=["aggregate_trade_id","price","quantity","first_trade_id","last_trade_id","timestamp","is_buyer_maker"]
KLINE_COLS=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"]
METRIC_REQUIRED={"create_time","sum_open_interest","sum_open_interest_value","count_toptrader_long_short_ratio","sum_toptrader_long_short_ratio","count_long_short_ratio","sum_taker_long_short_vol_ratio"}

@dataclass(frozen=True)
class DatasetSpec:
    name:str
    interval:str|None=None
    def relative_dir(self,cadence:str,symbol:str)->str:
        return f"{cadence}/{self.name}/{symbol}/{self.interval}" if self.interval else f"{cadence}/{self.name}/{symbol}"
    def filename(self,symbol:str,stamp:str)->str:
        return f"{symbol}-{self.interval}-{stamp}.zip" if self.interval else f"{symbol}-{self.name}-{stamp}.zip"

DATASETS={
 "aggTrades":DatasetSpec("aggTrades"),"markPriceKlines":DatasetSpec("markPriceKlines","15m"),
 "indexPriceKlines":DatasetSpec("indexPriceKlines","15m"),"premiumIndexKlines":DatasetSpec("premiumIndexKlines","15m"),
 "metrics":DatasetSpec("metrics")}

def http_bytes(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"*/*"}),timeout=60) as r:return r.read()
def fetch_verified_zip(url,verify=True):
    raw=http_bytes(url)
    if not verify:return raw
    expected=http_bytes(url+".CHECKSUM").decode(errors="replace").strip().split()[0].lower()
    actual=hashlib.sha256(raw).hexdigest()
    if len(expected)!=64 or actual!=expected:raise RuntimeError(f"checksum mismatch expected={expected!r} actual={actual}")
    return raw

def zip_csv(raw, names=None, preserve_header=False):
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        files=[n for n in zf.namelist() if n.lower().endswith('.csv')]
        if len(files)!=1:raise RuntimeError(f"expected one CSV, got {files}")
        with zf.open(files[0]) as f: df=pd.read_csv(f) if preserve_header else pd.read_csv(f,header=None)
    if preserve_header:return df
    # Binance archives may include a textual header row.
    if len(df) and not str(df.iloc[0,0]).replace('-','').isdigit():df=df.iloc[1:].reset_index(drop=True)
    if names:
        if df.shape[1]<len(names):raise RuntimeError(f"unexpected columns {df.shape[1]} < {len(names)}")
        df=df.iloc[:,:len(names)].copy();df.columns=names
    return df

def parse_ts(s):
    # Metrics use ISO timestamps; archive trades/klines use epoch units.
    numeric=pd.to_numeric(s,errors='coerce')
    if numeric.notna().mean()<0.8:return pd.to_datetime(s,utc=True,errors='coerce')
    med=float(numeric.dropna().median()) if numeric.notna().any() else 0
    unit='ns' if med>1e17 else ('us' if med>1e14 else 'ms')
    return pd.to_datetime(numeric,unit=unit,utc=True,errors='coerce')

def bools(s):return s if s.dtype==bool else s.astype(str).str.lower().isin({'true','1','t'})
def process_agg(df,freq):
    for c in ('price','quantity'):df[c]=pd.to_numeric(df[c],errors='coerce')
    df['timestamp']=parse_ts(df['timestamp']);df=df.dropna(subset=['timestamp','price','quantity']).set_index('timestamp').sort_index();m=bools(df['is_buyer_maker'])
    df['buy_quote']=np.where(~m,df.quantity*df.price,0.);df['sell_quote']=np.where(m,df.quantity*df.price,0.);df['total_quote']=df.quantity*df.price;df['delta_quote']=df.buy_quote-df.sell_quote
    g=df.resample(freq,label='left',closed='left').agg(trade_open=('price','first'),trade_high=('price','max'),trade_low=('price','min'),trade_close=('price','last'),buy_quote=('buy_quote','sum'),sell_quote=('sell_quote','sum'),total_quote=('total_quote','sum'),delta_quote=('delta_quote','sum'),agg_trade_count=('price','size'),avg_trade_quote=('total_quote','mean'))
    g['buy_share_quote']=g.buy_quote/g.total_quote.replace(0,np.nan);g['taker_buy_sell_ratio_quote']=g.buy_quote/g.sell_quote.replace(0,np.nan);g['cvd_quote']=g.delta_quote.cumsum();g['flow_imbalance_quote']=g.delta_quote/g.total_quote.replace(0,np.nan)
    return g

def process_kline(df,prefix,freq):
    for c in ('open','high','low','close','volume','quote_volume'):df[c]=pd.to_numeric(df[c],errors='coerce')
    df['open_time']=parse_ts(df.open_time);df=df.dropna(subset=['open_time']).set_index('open_time').sort_index()
    out=df[['open','high','low','close','volume','quote_volume']].resample(freq,label='left',closed='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum','quote_volume':'sum'});out.columns=[f'{prefix}_{c}' for c in out.columns];return out

def process_metrics(df,freq):
    df.columns=[str(c).strip().lower() for c in df.columns]
    missing=METRIC_REQUIRED-set(df.columns)
    if missing:raise RuntimeError(f"metrics schema missing {sorted(missing)}; got={list(df.columns)}")
    df['create_time']=parse_ts(df.create_time);df=df.dropna(subset=['create_time']).set_index('create_time').sort_index()
    cols=sorted(METRIC_REQUIRED-{'create_time'})
    for c in cols:df[c]=pd.to_numeric(df[c],errors='coerce')
    # Metrics are snapshots (normally 5m). Last observation in each research bar avoids averaging ratios/states.
    out=df[cols].resample(freq,label='left',closed='left').last()
    out=out.add_prefix('fut_')
    out['fut_oi_change_1bar']=out.fut_sum_open_interest.pct_change()
    out['fut_oi_value_change_1bar']=out.fut_sum_open_interest_value.pct_change()
    return out

def daily_url(spec,symbol,d):
    s=d.isoformat();return f"{BASE}/{spec.relative_dir('daily',symbol)}/{spec.filename(symbol,s)}"
def days(a,b):
    while a<=b:yield a;a+=timedelta(days=1)
def load(spec,symbol,start,end,checksum,preserve_header=False):
    parts=[];fail=[]
    names=AGG_COLS if spec.name=='aggTrades' else (KLINE_COLS if 'Klines' in spec.name else None)
    for d in days(start,end):
        url=daily_url(spec,symbol,d)
        try:parts.append(zip_csv(fetch_verified_zip(url,checksum),names,preserve_header))
        except urllib.error.HTTPError as e:fail.append({'dataset':spec.name,'date':d.isoformat(),'status':e.code,'url':url})
        except Exception as e:fail.append({'dataset':spec.name,'date':d.isoformat(),'error':f'{type(e).__name__}: {e}','url':url})
    return (pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()),fail

def build(symbol,start,end,freq,checksum):
    loaded={};fail=[]
    agg,bad=load(DATASETS['aggTrades'],symbol,start,end,checksum);fail+=bad;loaded['aggTrades']=len(agg)
    if agg.empty:raise RuntimeError('no aggTrades loaded')
    frame=process_agg(agg,freq)
    for name,prefix in [('markPriceKlines','mark'),('indexPriceKlines','index'),('premiumIndexKlines','premium')]:
        raw,bad=load(DATASETS[name],symbol,start,end,checksum);fail+=bad;loaded[name]=len(raw)
        if not raw.empty:frame=frame.join(process_kline(raw,prefix,freq),how='left')
    metrics,bad=load(DATASETS['metrics'],symbol,start,end,checksum,preserve_header=True);fail+=bad;loaded['metrics']=len(metrics)
    if metrics.empty:raise RuntimeError('no futures metrics loaded; fail-closed')
    frame=frame.join(process_metrics(metrics,freq),how='left')
    if {'mark_close','index_close'}<=set(frame.columns):frame['mark_index_basis_bps']=(frame.mark_close/frame.index_close-1)*10000
    frame['ret_1bar']=frame.trade_close.pct_change();frame['realized_range_bps']=(frame.trade_high/frame.trade_low-1)*10000
    frame.insert(0,'symbol',symbol);frame.index.name='ts';frame=frame.reset_index()
    metric_cols=[c for c in frame if c.startswith('fut_')]
    coverage={c:int(frame[c].notna().sum()) for c in metric_cols}
    if any(coverage[c]==0 for c in metric_cols):raise RuntimeError(f'zero metrics coverage: {coverage}')
    meta={'engine':'BINANCE_VISION_HISTORICAL_FEATURE_LAYER_V1_2','authorization':AUTHORIZATION,'liveTrading':False,'source':'Binance Public Data / data.binance.vision','symbol':symbol,'start':start.isoformat(),'end':end.isoformat(),'frequency':freq,'checksumVerification':checksum,'rows':len(frame),'rawRows':loaded,'metricsCoverage':coverage,'failures':fail,'featureSemantics':{'flowImbalanceQuote':'delta_quote / total_quote','markIndexBasisBps':'(mark_close/index_close-1)*10000','futuresMetrics':'Binance Vision metrics snapshots; last observation per research bar','microprice':'NOT AVAILABLE from these archives; requires L2','orderBookImbalance':'NOT AVAILABLE from these archives; requires L2'}}
    return frame,meta

def main():
    p=argparse.ArgumentParser();p.add_argument('--symbol',default='BTCUSDT');p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--freq',default='5min',choices=['1min','5min','15min','1h']);p.add_argument('--output-dir',default='artifacts/binance-vision-historical-features');p.add_argument('--no-checksum',action='store_true');a=p.parse_args()
    start=datetime.strptime(a.start,'%Y-%m-%d').date();end=datetime.strptime(a.end,'%Y-%m-%d').date();out=pathlib.Path(a.output_dir);out.mkdir(parents=True,exist_ok=True)
    frame,meta=build(a.symbol.upper().strip(),start,end,a.freq,not a.no_checksum);pq=out/f'{a.symbol.upper()}-{a.freq}-{start}_{end}.parquet';frame.to_parquet(pq,index=False);pq.with_suffix('.json').write_text(json.dumps(meta,indent=2,sort_keys=True))
    print(json.dumps({'kind':'historical_feature_layer_complete','authorization':AUTHORIZATION,'liveTrading':False,'rows':len(frame),'metricsCoverage':meta['metricsCoverage'],'failures':len(meta['failures'])},separators=(',',':')))
if __name__=='__main__':main()
