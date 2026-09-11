#!/usr/bin/env python3
import datetime, json, pathlib, time, urllib.parse, urllib.request, traceback, numpy as np
import jesse.helpers as jh
from jesse.research import backtest
from strategies.UnifiedCandidateValidator import UnifiedCandidateValidator

MANIFEST=json.loads(pathlib.Path("validation/fusion/candidate-manifest.json").read_text())
VALIDATION=MANIFEST.get("validation") or {}
HISTORICAL_SCOPE=VALIDATION.get("historicalScope","FULL_CANDIDATE")
HISTORICAL_EXCLUDES=VALIDATION.get("historicalExcludes",[])
if not MANIFEST.get("candidateFingerprint"):
    report={"engine":"JESSE","strategyId":"TST_CANDIDATE_JESSE_VALIDATOR_V1","status":"NO_CANDIDATE","pass":False,"candidateId":None,"candidateFingerprint":None,"candidateStatus":MANIFEST.get("status"),"validationScope":HISTORICAL_SCOPE,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"notes":"No unified candidate is active; Jesse exits without downloading market data."}
    pathlib.Path("validation/fusion/jesse-latest.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2)); raise SystemExit(0)
SYMBOL_API=MANIFEST["symbol"]
SYMBOL=SYMBOL_API[:-4]+"-USDT"
TF=MANIFEST["timeframe"]
EXCHANGE="Binance Spot Synthetic Feed"
STRATEGY_ID="TST_CANDIDATE_JESSE_VALIDATOR_V1"
DAYS=365

def get_json(url, user_agent, attempts=6):
    last=None
    for attempt in range(attempts):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":user_agent})
            with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
        except Exception as exc:
            last=exc
            if attempt+1<attempts: time.sleep(min(2**attempt,12))
    raise RuntimeError(f"market-data request failed after {attempts} attempts: {last}")

def fetch_1m(days=DAYS):
    end=int(time.time()*1000);start=end-days*86400000;out=[];cursor=start
    while cursor<end:
        qs=urllib.parse.urlencode({"symbol":SYMBOL_API,"interval":"1m","limit":1000,"startTime":cursor,"endTime":end})
        rows=get_json("https://data-api.binance.vision/api/v3/klines?"+qs,"tst-unified-jesse/1.3")
        if not rows:break
        for k in rows:out.append([float(k[0]),float(k[1]),float(k[4]),float(k[2]),float(k[3]),float(k[5])])
        nxt=int(rows[-1][0])+60000
        if nxt<=cursor:break
        cursor=nxt;time.sleep(0.01)
    return np.asarray(out,dtype=float)

def build_leader_map():
    if MANIFEST.get("family")!="CROSS_CRYPTO_LEAD_LAG":
        pathlib.Path("validation/fusion/jesse-leader.json").write_text("{}"); return
    end=int(time.time()*1000);start=end-DAYS*86400000;series={}
    for symbol in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
        rows=[];cursor=start
        while cursor<end:
            qs=urllib.parse.urlencode({"symbol":symbol,"interval":"1h","limit":1000,"startTime":cursor,"endTime":end})
            batch=get_json("https://data-api.binance.vision/api/v3/klines?"+qs,"tst-unified-jesse-leader/1.3")
            if not batch:break
            rows.extend(batch);nxt=int(batch[-1][0])+3600000
            if nxt<=cursor:break
            cursor=nxt;time.sleep(0.01)
        vals={}
        for i,row in enumerate(rows):
            if i<3:continue
            ts=int(row[0]);close=float(row[4]);prev=float(rows[i-3][4]);vals[ts]=close/prev-1
        series[symbol]=vals
    keys=set.intersection(*(set(v.keys()) for v in series.values())) if series else set()
    out={str(ts):sum(series[s][ts] for s in series)/len(series) for ts in keys}
    pathlib.Path("validation/fusion/jesse-leader.json").write_text(json.dumps(out));print(f"leader points: {len(out)}")

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
    cfg={"starting_balance":20.08,"fee":fee,"type":"spot","exchange":EXCHANGE,"warm_up_candles":0}
    routes=[{"exchange":EXCHANGE,"strategy":UnifiedCandidateValidator,"symbol":SYMBOL,"timeframe":TF}]
    cd={jh.key(EXCHANGE,SYMBOL):{"exchange":EXCHANGE,"symbol":SYMBOL,"candles":candles}}
    result=backtest(cfg,routes,[],candles=cd,generate_equity_curve=True,fast_mode=True)
    m=result.get("metrics") or {}
    n=int(metric(m,"total","total_trades","trades","count"));win=metric(m,"win_rate","winrate");netpct=metric(m,"net_profit_percentage","net_profit","total_profit");pf=metric(m,"profit_factor");maxdd=metric(m,"max_drawdown","max_drawdown_percentage")
    netusdt=(netpct/100*20.08) if abs(netpct)>1 else (netpct*20.08);ex=netusdt/n if n else 0
    return {"trades":n,"winRate":win,"profitFactor":pf,"expectancyUSDT":ex,"netPnlUSDT":netusdt,"maxDrawdown":maxdd}

def write_report(report):
    pathlib.Path("validation/fusion/jesse-latest.json").write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))

def scope_metadata():
    return {
        "validationScope":HISTORICAL_SCOPE,
        "fullStrategyValidated":False if HISTORICAL_SCOPE=="CORE_TRIGGER_ONLY" else None,
        "excludedConfirmations":HISTORICAL_EXCLUDES,
        "eligibleOutcome":"FORWARD_PAPER_COLLECTION_ONLY" if HISTORICAL_SCOPE=="CORE_TRIGGER_ONLY" else "CANDIDATE_VALIDATION",
    }

try:
    build_leader_map();candles=fetch_1m()
    if len(candles)<50000:raise RuntimeError(f"insufficient candles {len(candles)}")
    base=run(candles,0.0015);stress=run(candles,0.003)
    independent=base["trades"]>=30 and stress["trades"]>=30 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0
    passed=independent and base["trades"]>=100 and stress["trades"]>=100
    report={"engine":"JESSE","strategyId":STRATEGY_ID,"status":"PASS" if passed else "FAIL","pass":passed,"independentEnginePass":independent,"candidateId":MANIFEST.get("candidateId"),"candidateFingerprint":MANIFEST.get("candidateFingerprint"),"symbol":SYMBOL_API,"family":MANIFEST.get("family"),"timeframe":TF,"base":base,"stress2x":stress,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"notes":"Historical Jesse validation is explicitly limited to the candle-derived core trigger when validationScope=CORE_TRIGGER_ONLY. It does not validate L2/order-book confirmation and cannot authorize live trading."}
    report.update(scope_metadata());write_report(report)
except Exception as exc:
    report={"engine":"JESSE","strategyId":STRATEGY_ID,"status":"ERROR","pass":False,"independentEnginePass":False,"candidateId":MANIFEST.get("candidateId"),"candidateFingerprint":MANIFEST.get("candidateFingerprint"),"symbol":SYMBOL_API,"family":MANIFEST.get("family"),"timeframe":TF,"base":{"trades":0},"stress2x":{"trades":0},"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"error":f"{type(exc).__name__}: {exc}","traceback":traceback.format_exc()[-6000:],"notes":"Jesse infrastructure/backtest error. Snapshot is deliberately current-candidate and fail-closed so Fusion Gate cannot mistake stale results for this candidate."}
    report.update(scope_metadata());write_report(report);raise
