#!/usr/bin/env python3
import json, math, statistics, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = "REVERSAL_V1_2026-09-04"
SYMBOLS_DISCOVERY = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT"]
SYMBOLS_HOLDOUT = ["AVAXUSDT","SUIUSDT","APTUSDT","NEARUSDT"]
ALL_SYMBOLS = SYMBOLS_DISCOVERY + SYMBOLS_HOLDOUT
MAJORS = {"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"}
INTERVAL_MS = 15*60*1000
DAYS = 365
MAX_HOLD_BARS = 144
MIN_24H_QVOL = 20_000_000
BASE_FEE = 0.001
BASE_SLIP = 0.0005
STRESS_FEE = 0.001
STRESS_SLIP = 0.0026
MIN_NOTIONAL = 5.05
END_MS = int(datetime.now(timezone.utc).timestamp()*1000)
START_MS = int((datetime.now(timezone.utc)-timedelta(days=DAYS)).timestamp()*1000)
API_BASES = ["https://data-api.binance.vision","https://api.binance.com","https://api1.binance.com"]

FROZEN = {
  "stop_hunt": {"sweep":0.003,"reclaim_close_pos":0.60,"lower_wick_frac":0.30,"min_relvol":1.35,"rsi_max":48,"reward_r":2.4,"stop_buffer":0.004},
  "crash_exhaustion": {"drop_6bars":-0.045,"bounce_from_low":0.012,"range_expansion":1.8,"min_relvol":1.60,"rsi_max":42,"reward_r":2.8,"stop_buffer":0.006},
  "execution": {"max_hold_bars":MAX_HOLD_BARS,"same_bar":"STOP_FIRST","fee_each_side":BASE_FEE,"slippage_each_side":BASE_SLIP,"stress_slippage_each_side":STRESS_SLIP},
  "liquidity": {"rolling_24h_quote_volume_min":MIN_24H_QVOL},
  "release_gate": {"min_trades":100,"oos_expectancy_r_gt":0,"oos_pf_min":1.2,"positive_walkforward_folds_min":4,"walkforward_folds":5,"stress_expectancy_r_gt":0}
}

def api_get(path, params):
    qs = urllib.parse.urlencode(params)
    last = None
    for base in API_BASES:
        try:
            req = urllib.request.Request(base+path+"?"+qs, headers={"User-Agent":"tst-spot-signal/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(0.15)
    raise RuntimeError(last)

def fetch_klines(symbol):
    out=[]; cursor=START_MS
    while cursor < END_MS:
        rows=api_get("/api/v3/klines",{"symbol":symbol,"interval":"15m","startTime":cursor,"endTime":END_MS,"limit":1000})
        if not rows: break
        for k in rows:
            if int(k[6]) >= END_MS: continue
            out.append({"t":int(k[0]),"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"qv":float(k[7]),"tbq":float(k[10])})
        nxt=int(rows[-1][0])+INTERVAL_MS
        if nxt<=cursor: break
        cursor=nxt
        if len(rows)<1000: break
        time.sleep(0.08)
    dedup={x["t"]:x for x in out}
    return [dedup[t] for t in sorted(dedup)]

def median(xs):
    return statistics.median(xs) if xs else 0.0

def rsi14(bars,i):
    if i<14: return 50.0
    g=l=0.0
    for j in range(i-13,i+1):
        d=bars[j]["c"]-bars[j-1]["c"]
        if d>0: g+=d
        else: l-=d
    if l==0: return 100.0
    rs=(g/14)/(l/14)
    return 100-100/(1+rs)

def context(bars,i):
    if i<30: return None
    rel_base=median([x["qv"] for x in bars[i-24:i]])
    relvol=bars[i]["qv"]/rel_base if rel_base>0 else 0
    flow=bars[i-7:i+1]
    total=sum(x["qv"] for x in flow)
    taker=sum(x["tbq"] for x in flow)/total if total>0 else 0
    qv24=sum(x["qv"] for x in bars[max(0,i-95):i+1])
    return relvol,taker,qv24,rsi14(bars,i)

def lane_cfg(symbol,qv24):
    base=symbol[:-4]
    large=(base in MAJORS) or qv24>150_000_000
    if large: return {"max_pos":7.0,"max_risk":0.20,"max_stop_pct":2.5,"min_taker_stop":0.53,"min_taker_crash":0.54}
    return {"max_pos":5.5,"max_risk":0.10,"max_stop_pct":2.2,"min_taker_stop":0.54,"min_taker_crash":0.55}

def stop_hunt_signal(bars,i,ctx,cfg):
    relvol,taker,qv24,rsi=ctx
    last=bars[i]; prior=bars[i-21:i-1]
    if len(prior)<20: return None
    prior_low=min(x["l"] for x in prior)
    rng=max(last["h"]-last["l"],1e-12)
    lower=max(0,min(last["o"],last["c"])-last["l"])
    ok=(last["l"]<prior_low*(1-FROZEN["stop_hunt"]["sweep"]) and last["c"]>prior_low and last["c"]>last["o"]
        and (last["c"]-last["l"])/rng>=FROZEN["stop_hunt"]["reclaim_close_pos"] and lower/rng>=FROZEN["stop_hunt"]["lower_wick_frac"]
        and relvol>=FROZEN["stop_hunt"]["min_relvol"] and taker>=cfg["min_taker_stop"] and rsi<=FROZEN["stop_hunt"]["rsi_max"])
    if not ok: return None
    return {"strategy":"STOP_HUNT_REVERSAL","stop_raw":last["l"]*(1-FROZEN["stop_hunt"]["stop_buffer"]),"reward_r":FROZEN["stop_hunt"]["reward_r"],"signal_t":last["t"],"relvol":relvol,"taker":taker,"rsi":rsi}

def crash_signal(bars,i,ctx,cfg):
    relvol,taker,qv24,rsi=ctx
    if i<30: return None
    last=bars[i]; recent=bars[i-5:i+1]; anchor=bars[i-6]
    prior_ranges=[x["h"]-x["l"] for x in bars[i-29:i-5] if x["h"]>x["l"]]
    normal=median(prior_ranges); max_rng=max(x["h"]-x["l"] for x in recent); low=min(x["l"] for x in recent)
    drop=last["c"]/anchor["c"]-1 if anchor["c"]>0 else 0
    ok=(drop<=FROZEN["crash_exhaustion"]["drop_6bars"] and low>0 and (last["c"]-low)/low>=FROZEN["crash_exhaustion"]["bounce_from_low"]
        and normal>0 and max_rng>=normal*FROZEN["crash_exhaustion"]["range_expansion"] and last["c"]>last["o"]
        and relvol>=FROZEN["crash_exhaustion"]["min_relvol"] and taker>=cfg["min_taker_crash"] and rsi<=FROZEN["crash_exhaustion"]["rsi_max"])
    if not ok: return None
    return {"strategy":"LIQUIDITY_CRASH_EXHAUSTION","stop_raw":low*(1-FROZEN["crash_exhaustion"]["stop_buffer"]),"reward_r":FROZEN["crash_exhaustion"]["reward_r"],"signal_t":last["t"],"relvol":relvol,"taker":taker,"rsi":rsi}

def simulate_trade(symbol,bars,i,sig,fee,slip):
    if i+1>=len(bars): return None
    ctx=context(bars,i); cfg=lane_cfg(symbol,ctx[2])
    entry=bars[i+1]["o"]*(1+slip); stop=sig["stop_raw"]
    if not (entry>stop>0): return None
    risk_unit=entry-stop; stop_pct=risk_unit/entry*100
    if stop_pct>cfg["max_stop_pct"]: return None
    qty=min(cfg["max_pos"]/entry,cfg["max_risk"]/risk_unit)
    notional=qty*entry
    if notional<MIN_NOTIONAL: return None
    target=entry+sig["reward_r"]*risk_unit
    initial_risk=qty*risk_unit
    max_h=entry; min_l=entry; exit_px=None; reason=None; exit_i=None
    last_j=min(len(bars)-1,i+MAX_HOLD_BARS)
    for j in range(i+1,last_j+1):
        b=bars[j]; max_h=max(max_h,b["h"]); min_l=min(min_l,b["l"])
        hit_stop=b["l"]<=stop; hit_target=b["h"]>=target
        if hit_stop and hit_target: exit_px=stop*(1-slip); reason="STOP_FIRST"; exit_i=j; break
        if hit_stop: exit_px=stop*(1-slip); reason="STOP"; exit_i=j; break
        if hit_target: exit_px=target*(1-slip); reason="TARGET"; exit_i=j; break
    if exit_px is None:
        exit_i=last_j; exit_px=bars[exit_i]["c"]*(1-slip); reason="TIME"
    gross=qty*(exit_px-entry); fees=qty*entry*fee+qty*exit_px*fee; pnl=gross-fees
    net_r=pnl/initial_risk if initial_risk>0 else 0
    mfe=(max_h-entry)/risk_unit; mae=(entry-min_l)/risk_unit
    return {"symbol":symbol,"strategy":sig["strategy"],"signal_t":sig["signal_t"],"entry_t":bars[i+1]["t"],"exit_t":bars[exit_i]["t"],"entry":entry,"stop":stop,"target":target,"reason":reason,"qty":qty,"notional":notional,"pnl":pnl,"net_r":net_r,"mfe_r":mfe,"mae_r":mae,"relvol":sig["relvol"],"taker":sig["taker"],"rsi":sig["rsi"],"stop_pct":stop_pct}

def backtest_symbol(symbol,bars,fee,slip):
    trades=[]; i=40
    while i < len(bars)-2:
        ctx=context(bars,i)
        if not ctx or ctx[2]<MIN_24H_QVOL: i+=1; continue
        cfg=lane_cfg(symbol,ctx[2])
        sig=stop_hunt_signal(bars,i,ctx,cfg)
        if sig is None: sig=crash_signal(bars,i,ctx,cfg)
        if sig:
            tr=simulate_trade(symbol,bars,i,sig,fee,slip)
            if tr:
                trades.append(tr)
                exit_idx=next((k for k in range(i+1,min(len(bars),i+MAX_HOLD_BARS+2)) if bars[k]["t"]==tr["exit_t"]),i+1)
                i=max(i+1,exit_idx+1); continue
        i+=1
    return trades

def metrics(trades):
    if not trades: return {"trades":0,"win_rate":0,"profit_factor":0,"expectancy_r":0,"median_r":0,"net_pnl_usdt":0,"max_drawdown_usdt":0,"median_mfe_mae_ratio":0}
    wins=[t for t in trades if t["pnl"]>0]; gp=sum(t["pnl"] for t in wins); gl=sum(-t["pnl"] for t in trades if t["pnl"]<0)
    eq=peak=0; dd=0
    for t in sorted(trades,key=lambda x:x["exit_t"]):
        eq+=t["pnl"]; peak=max(peak,eq); dd=max(dd,peak-eq)
    med_mfe=median([t["mfe_r"] for t in trades]); med_mae=median([t["mae_r"] for t in trades]); ratio=med_mfe/med_mae if med_mae>0 else 999
    return {"trades":len(trades),"wins":len(wins),"win_rate":round(100*len(wins)/len(trades),2),"profit_factor":round(gp/gl,3) if gl>0 else (999 if gp>0 else 0),"expectancy_r":round(sum(t["net_r"] for t in trades)/len(trades),4),"median_r":round(median([t["net_r"] for t in trades]),4),"net_pnl_usdt":round(sum(t["pnl"] for t in trades),4),"max_drawdown_usdt":round(dd,4),"median_mfe_r":round(med_mfe,3),"median_mae_r":round(med_mae,3),"median_mfe_mae_ratio":round(ratio,3)}

def split_oos(trades):
    if not trades: return [],[]
    ts=sorted(t["signal_t"] for t in trades); cutoff=ts[int(len(ts)*0.70)]
    return [t for t in trades if t["signal_t"]<cutoff],[t for t in trades if t["signal_t"]>=cutoff]

def walkforward(trades,n=5):
    if not trades: return []
    ts=[t["signal_t"] for t in trades]; lo=min(ts); hi=max(ts)+1; width=max(1,(hi-lo)//n); out=[]
    for k in range(n):
        a=lo+k*width; b=hi if k==n-1 else lo+(k+1)*width
        seg=[t for t in trades if a<=t["signal_t"]<b]
        out.append(metrics(seg))
    return out

def grouped(trades):
    out={}
    for s in ["STOP_HUNT_REVERSAL","LIQUIDITY_CRASH_EXHAUSTION"]:
        out[s]=metrics([t for t in trades if t["strategy"]==s])
    return out

def build_report(base,stress,availability):
    ins,oos=split_oos(base); sins,soos=split_oos(stress)
    wf=walkforward(base,5); pos=sum(1 for x in wf if x.get("expectancy_r",0)>0)
    gate={
      "min_trades":len(base)>=100,
      "oos_expectancy_positive":metrics(oos)["expectancy_r"]>0,
      "oos_profit_factor_ge_1_2":metrics(oos)["profit_factor"]>=1.2,
      "walkforward_positive_folds_ge_4":pos>=4,
      "stress_expectancy_positive":metrics(stress)["expectancy_r"]>0,
    }
    gate["historical_pass"]=all(gate.values())
    return {
      "generatedAt":datetime.now(timezone.utc).isoformat(),"definitionVersion":VERSION,"source":"Binance Spot 15m closed OHLCV + taker buy quote","days":DAYS,
      "symbols":{"discovery":SYMBOLS_DISCOVERY,"holdout":SYMBOLS_HOLDOUT,"availability":availability},"frozenDefinition":FROZEN,
      "limitations":["Historical order-book depth/spread/microprice are unavailable here and remain forward-paper gates.","This validator enters next-bar open to avoid look-ahead.","No parameter optimization is performed after results."],
      "base":{"all":metrics(base),"byStrategy":grouped(base),"discovery":metrics([t for t in base if t["symbol"] in SYMBOLS_DISCOVERY]),"symbolHoldout":metrics([t for t in base if t["symbol"] in SYMBOLS_HOLDOUT]),"chronologicalInSample":metrics(ins),"chronologicalOOS":metrics(oos),"walkforward":wf,"positiveFolds":pos},
      "stress":{"all":metrics(stress),"byStrategy":grouped(stress),"chronologicalOOS":metrics(soos)},"gate":gate,
      "releaseApproved":False,"releaseNote":"Historical pass can only promote these strategies to short forward-paper microstructure validation; it cannot auto-enable live trading."
    }

def main():
    availability={}; all_base=[]; all_stress=[]
    cache={}
    for symbol in ALL_SYMBOLS:
        print("Downloading",symbol,flush=True)
        try:
            bars=fetch_klines(symbol); cache[symbol]=bars; availability[symbol]=len(bars)
            print(symbol,len(bars),"bars",flush=True)
            all_base.extend(backtest_symbol(symbol,bars,BASE_FEE,BASE_SLIP))
            all_stress.extend(backtest_symbol(symbol,bars,STRESS_FEE,STRESS_SLIP))
        except Exception as e:
            availability[symbol]={"error":str(e)}
    report=build_report(all_base,all_stress,availability)
    out=Path("validation/reversal"); out.mkdir(parents=True,exist_ok=True)
    (out/"latest.json").write_text(json.dumps(report,indent=2,ensure_ascii=False))
    lines=["# Reversal Historical Validation",f"Version: `{VERSION}`",f"Generated: {report['generatedAt']}","", "## Base",json.dumps(report["base"],indent=2),"", "## Stress",json.dumps(report["stress"],indent=2),"", "## Gate",json.dumps(report["gate"],indent=2),"", "**Live remains disabled.**"]
    (out/"latest.md").write_text("\n".join(lines))
    print(json.dumps(report["gate"],indent=2))

if __name__=="__main__": main()
