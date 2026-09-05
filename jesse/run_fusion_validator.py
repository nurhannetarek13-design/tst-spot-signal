#!/usr/bin/env python3
import datetime, json, pathlib, time, urllib.parse, urllib.request, numpy as np
import jesse.helpers as jh
from jesse.research import backtest
from strategies.UnifiedCandidateValidator import UnifiedCandidateValidator

MANIFEST=json.loads(pathlib.Path("validation/fusion/frozen-parity-candidate.json").read_text())
FIXTURE=json.loads(pathlib.Path("validation/fusion/execution-parity-fixture.json").read_text())
if not MANIFEST.get("candidateFingerprint"):
    report={"engine":"JESSE","strategyId":"TST_CANDIDATE_JESSE_VALIDATOR_V1","status":"NO_CANDIDATE","pass":False,"candidateId":None,"candidateFingerprint":None,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat()}
    pathlib.Path("validation/fusion/jesse-latest.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    raise SystemExit(0)
SYMBOL_API=MANIFEST["symbol"]
SYMBOL=SYMBOL_API[:-4]+"-USDT"
TF=MANIFEST["timeframe"]
EXCHANGE="Binance Spot Synthetic Feed"
STRATEGY_ID="TST_CANDIDATE_JESSE_VALIDATOR_V1"

def iso_ms(s):
    return int(datetime.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()*1000)

DATA_START_MS=iso_ms(FIXTURE["dataset"]["firstTs"])
BAR_MS=3600000 if TF=="1h" else 900000
DATA_END_MS=iso_ms(FIXTURE["dataset"]["lastTs"])+BAR_MS-1

def fetch_1m():
    end=DATA_END_MS;start=DATA_START_MS;out=[];cursor=start
    while cursor<=end:
        qs=urllib.parse.urlencode({"symbol":SYMBOL_API,"interval":"1m","limit":1000,"startTime":cursor,"endTime":end})
        req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-unified-jesse/1.1"})
        with urllib.request.urlopen(req,timeout=20) as r:rows=json.load(r)
        if not rows:break
        for k in rows:out.append([float(k[0]),float(k[1]),float(k[4]),float(k[2]),float(k[3]),float(k[5])])
        nxt=int(rows[-1][0])+60000
        if nxt<=cursor:break
        cursor=nxt;time.sleep(0.01)
    return np.asarray(out,dtype=float)

def build_leader_map():
    if MANIFEST.get("family")!="CROSS_CRYPTO_LEAD_LAG":
        pathlib.Path("validation/fusion/jesse-leader.json").write_text("{}")
        return
    start=DATA_START_MS;end=DATA_END_MS
    series={}
    for symbol in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
        rows=[];cursor=start
        while cursor<=end:
            qs=urllib.parse.urlencode({"symbol":symbol,"interval":"1h","limit":1000,"startTime":cursor,"endTime":end})
            req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-unified-jesse-leader/1.1"})
            with urllib.request.urlopen(req,timeout=20) as r:batch=json.load(r)
            if not batch:break
            rows.extend(batch);nxt=int(batch[-1][0])+3600000
            if nxt<=cursor:break
            cursor=nxt;time.sleep(0.01)
        vals={}
        for i,row in enumerate(rows):
            if i<3:continue
            ts=int(row[0]);close=float(row[4]);prev=float(rows[i-3][4])
            vals[ts]=close/prev-1
        series[symbol]=vals
    keys=set.intersection(*(set(v.keys()) for v in series.values())) if series else set()
    out={str(ts):sum(series[s][ts] for s in series)/len(series) for ts in keys}
    pathlib.Path("validation/fusion/jesse-leader.json").write_text(json.dumps(out))
    print(f"leader points: {len(out)}")

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

build_leader_map()
candles=fetch_1m()
expected_minutes=int((DATA_END_MS-DATA_START_MS+1)/60000)
if len(candles)<expected_minutes*0.995:raise RuntimeError(f"insufficient frozen-window candles {len(candles)} expected~{expected_minutes}")
base=run(candles,0.0015);stress=run(candles,0.003)
independent=base["trades"]>=30 and stress["trades"]>=30 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0
passed=independent and base["trades"]>=100 and stress["trades"]>=100
report={"engine":"JESSE","strategyId":STRATEGY_ID,"status":"PASS" if passed else "FAIL","pass":passed,"independentEnginePass":independent,"candidateId":MANIFEST.get("candidateId"),"candidateFingerprint":MANIFEST.get("candidateFingerprint"),"symbol":SYMBOL_API,"family":MANIFEST.get("family"),"timeframe":TF,"dataset":{"firstTs":FIXTURE["dataset"]["firstTs"],"lastTs":FIXTURE["dataset"]["lastTs"],"sha256":FIXTURE["dataset"]["sha256"]},"base":base,"stress2x":stress,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"notes":"Independent Jesse validation of frozen parity candidate on the exact fixture dataset window; long-only; 5.5 USDT stake."}
pathlib.Path("validation/fusion/jesse-latest.json").write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
