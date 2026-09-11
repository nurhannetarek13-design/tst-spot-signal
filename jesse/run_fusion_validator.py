#!/usr/bin/env python3
import datetime, json, pathlib, time, urllib.parse, urllib.request, numpy as np
import jesse.helpers as jh
from jesse.research import backtest
from strategies.UnifiedCandidateValidator import UnifiedCandidateValidator

MANIFEST=json.loads(pathlib.Path("validation/fusion/frozen-parity-candidate.json").read_text())
FIXTURE=json.loads(pathlib.Path("validation/fusion/execution-parity-fixture.json").read_text())
OUT=pathlib.Path("validation/fusion/jesse-latest.json")
if not MANIFEST.get("candidateFingerprint"):
    report={"engine":"JESSE","strategyId":"TST_CANDIDATE_JESSE_VALIDATOR_V1","status":"NO_CANDIDATE","pass":False,"candidateId":None,"candidateFingerprint":None,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat()}
    OUT.write_text(json.dumps(report,indent=2))
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
    end=DATA_END_MS;out=[];cursor=DATA_START_MS
    while cursor<=end:
        qs=urllib.parse.urlencode({"symbol":SYMBOL_API,"interval":"1m","limit":1000,"startTime":cursor,"endTime":end})
        req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-unified-jesse/1.2"})
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
            req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-unified-jesse-leader/1.2"})
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
    n=int(metric(m,"total","total_trades","trades","count"))
    win=metric(m,"win_rate","winrate")
    netusdt=metric(m,"net_profit")
    gross_profit=metric(m,"gross_profit")
    gross_loss=metric(m,"gross_loss")
    pf=(gross_profit/abs(gross_loss)) if gross_loss != 0 else (999.0 if gross_profit>0 else 0.0)
    ex=metric(m,"expectancy")
    if ex == 0.0 and n:
        ex=netusdt/n
    maxdd=metric(m,"max_drawdown","max_drawdown_percentage")
    return {"trades":n,"winRate":win,"profitFactor":pf,"expectancyUSDT":ex,"netPnlUSDT":netusdt,"grossProfitUSDT":gross_profit,"grossLossUSDT":gross_loss,"maxDrawdown":maxdd}

def base_report(status, note, **extra):
    row={"engine":"JESSE","strategyId":STRATEGY_ID,"status":status,"pass":False,"independentEnginePass":False,"candidateId":MANIFEST.get("candidateId"),"candidateFingerprint":MANIFEST.get("candidateFingerprint"),"symbol":SYMBOL_API,"family":MANIFEST.get("family"),"timeframe":TF,"dataset":{"firstTs":FIXTURE["dataset"]["firstTs"],"lastTs":FIXTURE["dataset"]["lastTs"],"sha256":FIXTURE["dataset"]["sha256"]},"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"notes":note}
    row.update(extra)
    return row

build_leader_map()
try:
    candles=fetch_1m()
except Exception as e:
    report=base_report("DATA_FETCH_FAIL",f"Jesse validation blocked by 1m data fetch error: {type(e).__name__}: {e}")
    OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2));raise SystemExit(0)

expected_minutes=int((DATA_END_MS-DATA_START_MS+1)/60000)
coverage=(len(candles)/expected_minutes) if expected_minutes else 0.0
if len(candles)<expected_minutes*0.995:
    report=base_report("DATA_QUALITY_FAIL",f"Fresh Jesse run intentionally not scored because frozen-window 1m coverage is below 99.5%.",dataQuality={"actual1mCandles":int(len(candles)),"expected1mCandles":int(expected_minutes),"coverage":coverage,"requiredCoverage":0.995})
    OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2));raise SystemExit(0)

try:
    base=run(candles,0.0015);stress=run(candles,0.003)
except Exception as e:
    report=base_report("ENGINE_FAIL",f"Fresh Jesse engine run failed safely: {type(e).__name__}: {e}",dataQuality={"actual1mCandles":int(len(candles)),"expected1mCandles":int(expected_minutes),"coverage":coverage,"requiredCoverage":0.995})
    OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2));raise SystemExit(0)

independent=base["trades"]>=30 and stress["trades"]>=30 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0
passed=independent and base["trades"]>=100 and stress["trades"]>=100
report=base_report("PASS" if passed else "FAIL","Independent Jesse validation of frozen parity candidate on the exact fixture dataset window; long-only; 5.5 USDT stake. Profit factor is derived from Jesse gross_profit/gross_loss; PnL and expectancy use Jesse native USD metrics.",base=base,stress2x=stress,dataQuality={"actual1mCandles":int(len(candles)),"expected1mCandles":int(expected_minutes),"coverage":coverage,"requiredCoverage":0.995})
report["pass"]=passed;report["independentEnginePass"]=independent
OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
