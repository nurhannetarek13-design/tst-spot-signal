#!/usr/bin/env python3
"""Engine-agnostic canonical execution adapter.

Consumes a frozen OHLCV dataset plus signal timestamps and applies exactly one
execution contract: next-bar-open entry, SL-first intrabar collision handling,
fixed gross TP/SL, fixed bar hold, and no signal reuse while a position is open.
Research-only: this module never places orders.
"""
import argparse, json, pathlib
import pandas as pd


def load_bars(path):
    p=pathlib.Path(path)
    if p.suffix.lower()=='.json':
        d=json.loads(p.read_text())
        rows=d.get('bars', d) if isinstance(d,dict) else d
        df=pd.DataFrame(rows)
    else:
        df=pd.read_csv(p)
    tscol=next((c for c in ['ts','timestamp','date','open_time'] if c in df.columns),None)
    if not tscol: raise ValueError('dataset needs ts/timestamp/date/open_time')
    if tscol=='open_time' and pd.api.types.is_numeric_dtype(df[tscol]):
        df['ts']=pd.to_datetime(df[tscol],unit='ms',utc=True)
    else: df['ts']=pd.to_datetime(df[tscol],utc=True)
    for c in ['open','high','low','close']:
        df[c]=pd.to_numeric(df[c],errors='raise')
    return df.sort_values('ts').drop_duplicates('ts').set_index('ts')


def load_signals(path):
    d=json.loads(pathlib.Path(path).read_text())
    if isinstance(d,dict): d=d.get('signals',d.get('signalTimestamps',[]))
    return {pd.Timestamp(x).tz_convert('UTC') if pd.Timestamp(x).tzinfo else pd.Timestamp(x).tz_localize('UTC') for x in d}


def simulate(df, signals, *, stake, fee, sl, tp, hold):
    idx=df.index; trades=[]; i=0
    while i < len(df)-1:
        if idx[i] not in signals:
            i+=1; continue
        entry_i=i+1
        entry=float(df.open.iloc[entry_i]); stop=entry*(1-sl); target=entry*(1+tp)
        exit_i=min(len(df)-1, entry_i+max(int(hold),1)); exit_price=float(df.close.iloc[exit_i]); reason='TIME'
        for j in range(entry_i,exit_i+1):
            lo=float(df.low.iloc[j]); hi=float(df.high.iloc[j])
            hit_sl=lo<=stop; hit_tp=hi>=target
            if hit_sl:
                exit_i=j; exit_price=stop; reason='SL_AMBIGUOUS_CONSERVATIVE' if hit_tp else 'SL'; break
            if hit_tp:
                exit_i=j; exit_price=target; reason='TP'; break
        qty=stake/entry; gross=qty*(exit_price-entry); fees=stake*fee+qty*exit_price*fee
        trades.append({'signalTs':idx[i].isoformat(),'entryTs':idx[entry_i].isoformat(),'exitTs':idx[exit_i].isoformat(),'entryPrice':entry,'exitPrice':exit_price,'reason':reason,'grossPnlUSDT':gross,'feesUSDT':fees,'pnlUSDT':gross-fees})
        # Canonical state rule: all signals on bars through the exit bar are suppressed.
        i=exit_i+1
    return trades


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dataset',required=True); ap.add_argument('--signals',required=True); ap.add_argument('--out',required=True)
    ap.add_argument('--stake',type=float,default=5.5); ap.add_argument('--fee',type=float,default=.0015)
    ap.add_argument('--sl',type=float,default=.03); ap.add_argument('--tp',type=float,default=.06); ap.add_argument('--hold',type=int,default=24)
    a=ap.parse_args(); df=load_bars(a.dataset); sig=load_signals(a.signals)
    trades=simulate(df,sig,stake=a.stake,fee=a.fee,sl=a.sl,tp=a.tp,hold=a.hold)
    out={'adapter':'TST_CANONICAL_EXECUTION_V1','execution':{'entry':'NEXT_BAR_OPEN','intrabarCollision':'SL_FIRST','signalSuppression':'THROUGH_EXIT_BAR','sl':a.sl,'tp':a.tp,'holdBars':a.hold,'feePerSide':a.fee,'stakeUSDT':a.stake},'trades':trades,'tradeCount':len(trades),'authorization':'RESEARCH_ONLY','liveTrading':False}
    pathlib.Path(a.out).write_text(json.dumps(out,indent=2)); print(json.dumps({'tradeCount':len(trades),'out':a.out},indent=2))

if __name__=='__main__': main()
