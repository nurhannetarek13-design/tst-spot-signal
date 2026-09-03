#!/usr/bin/env python3
import datetime, json, pathlib, time, urllib.parse, urllib.request, numpy as np
import jesse.helpers as jh
from jesse.research import backtest
from strategies.UnifiedCandidateValidator import UnifiedCandidateValidator

MANIFEST=json.loads(pathlib.Path("validation/fusion/candidate-manifest.json").read_text())
SYMBOL_API=MANIFEST["symbol"]
SYMBOL=SYMBOL_API[:-4]+"-USDT"
TF=MANIFEST["timeframe"]
EXCHANGE="Binance Spot Synthetic Feed"
STRATEGY_ID="TST_CANDIDATE_JESSE_VALIDATOR_V1"
DAYS=90

def fetch_1m(days=DAYS):
    end=int(time.time()*1000);start=end-days*86400000;out=[];cursor=start
    while cursor<end:
        qs=urllib.parse.urlencode({"symbol":SYMBOL_API,"interval":"1m","limit":1000,"startTime":cursor,"endTime":end})
        req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-unified-jesse/1.0"})
        with urllib.request.urlopen(req,timeout=20) as r:rows=json.load(r)
        if not rows:break
        for k in rows:out.append([float(k[0]),float(k[1]),float(k[4]),float(k[2]),float(k[3]),float(k[5])])
        nxt=int(rows[-1][0])+60000
        if nxt<=cursor:break
        cursor=nxt;time.sleep(0.01)
    return np.asarray(out,dtype=float)

def metric(metrics,*names):
    if not isinstance(metrics,dict):return 0.0
    low={str(k).lower().replace(" ","_"):v for k,v in metrics.items()}
    for n in names:
        k=n.lower().replace(" ","_")
        if k in low:
            try:return float(low[k] or 0)
            except:pass
    return 0.0

def run(candles,fee):
    cfg={"starting_balance":20.08,"fee":fee,"type":"futures","futures_leverage":1,"futures_leverage_mode":"cross","exchange":EXCHANGE,"warm_up_candles":0}
    routes=[{"exchange":EXCHANGE,"strategy":UnifiedCandidateValidator,"symbol":SYMBOL,"timeframe":TF}]
    cd={jh.key(EXCHANGE,SYMBOL):{"exchange":EXCHANGE,"symbol":SYMBOL,"candles":candles}}
    result=backtest(cfg,routes,[],candles=cd,generate_equity_curve=True,fast_mode=True)
    m=result.get("metrics") or {}
    n=int(metric(m,"total","total_trades","trades","count"));win=metric(m,"win_rate","winrate");netpct=metric(m,"net_profit_percentage","net_profit","total_profit");pf=metric(m,"profit_factor");maxdd=metric(m,"max_drawdown","max_drawdown_percentage")
    netusdt=(netpct/100*20.08) if abs(netpct)>1 else (netpct*20.08);ex=netusdt/n if n else 0
    return {"trades":n,"winRate":win,"profitFactor":pf,"expectancyUSDT":ex,"netPnlUSDT":netusdt,"maxDrawdown":maxdd}

candles=fetch_1m()
if len(candles)<50000:raise RuntimeError(f"insufficient candles {len(candles)}")
base=run(candles,0.0015);stress=run(candles,0.003)
independent=base["trades"]>=30 and stress["trades"]>=30 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0
passed=independent and base["trades"]>=40
report={"engine":"JESSE","strategyId":STRATEGY_ID,"status":"PASS" if passed else "FAIL","pass":passed,"independentEnginePass":independent,"candidateId":MANIFEST.get("candidateId"),"candidateFingerprint":MANIFEST.get("candidateFingerprint"),"symbol":SYMBOL_API,"family":MANIFEST.get("family"),"timeframe":TF,"base":base,"stress2x":stress,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"notes":"Independent Jesse validation of exact unified candidate; long-only; 1x simulator only for bracket exits."}
pathlib.Path("validation/fusion/jesse-latest.json").write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
