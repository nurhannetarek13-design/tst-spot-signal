#!/usr/bin/env python3
import datetime, json, pathlib, time, urllib.parse, urllib.request, numpy as np
import jesse.helpers as jh
from jesse.research import backtest
from strategies.AdaptiveRegimeFusionValidator import AdaptiveRegimeFusionValidator

SYMBOL_API="BTCUSDT"
SYMBOL="BTC-USDT"
EXCHANGE="Binance Spot Synthetic Feed"
TF="15m"
STRATEGY_ID="TST_ADAPTIVE_FUSION_V1"

def fetch_1m(days=35):
    end=int(time.time()*1000)
    start=end-days*24*60*60*1000
    out=[]
    cursor=start
    while cursor<end:
        qs=urllib.parse.urlencode({"symbol":SYMBOL_API,"interval":"1m","limit":1000,"startTime":cursor,"endTime":end})
        req=urllib.request.Request("https://data-api.binance.vision/api/v3/klines?"+qs,headers={"User-Agent":"tst-jesse-validator/1.0"})
        with urllib.request.urlopen(req,timeout=20) as r: rows=json.load(r)
        if not rows: break
        for k in rows:
            out.append([float(k[0]),float(k[1]),float(k[4]),float(k[2]),float(k[3]),float(k[5])])
        nxt=int(rows[-1][0])+60000
        if nxt<=cursor: break
        cursor=nxt
        time.sleep(0.03)
    return np.asarray(out,dtype=float)

def metric(metrics,*names):
    if not isinstance(metrics,dict): return 0.0
    low={str(k).lower().replace(" ","_"):v for k,v in metrics.items()}
    for n in names:
        k=n.lower().replace(" ","_")
        if k in low:
            try: return float(low[k] or 0)
            except: pass
    return 0.0

def run(candles,fee):
    cfg={
      "starting_balance":20.08,
      "fee":fee,
      "type":"futures",
      "futures_leverage":1,
      "futures_leverage_mode":"cross",
      "exchange":EXCHANGE,
      "warm_up_candles":0
    }
    routes=[{"exchange":EXCHANGE,"strategy":AdaptiveRegimeFusionValidator,"symbol":SYMBOL,"timeframe":TF}]
    cd={jh.key(EXCHANGE,SYMBOL):{"exchange":EXCHANGE,"symbol":SYMBOL,"candles":candles}}
    result=backtest(cfg,routes,[],candles=cd,generate_equity_curve=True,fast_mode=True)
    m=result.get("metrics") or {}
    n=int(metric(m,"total","total_trades","trades","count"))
    win=metric(m,"win_rate","winrate")
    netpct=metric(m,"net_profit_percentage","net_profit","total_profit")
    pf=metric(m,"profit_factor")
    maxdd=metric(m,"max_drawdown","max_drawdown_percentage")
    # Jesse metrics may expose percentages rather than absolute USDT; preserve a conservative conversion.
    netusdt=(netpct/100.0*20.08) if abs(netpct)>1 else (netpct*20.08)
    expectancy=(netusdt/n if n else 0.0)
    return {"trades":n,"winRate":win,"profitFactor":pf,"expectancyUSDT":expectancy,"netPnlUSDT":netusdt,"maxDrawdown":maxdd}

candles=fetch_1m(35)
if len(candles)<10000:
    raise RuntimeError(f"insufficient Binance candles: {len(candles)}")
base=run(candles,0.001)
stress=run(candles,0.002)
passed=base["trades"]>=100 and stress["trades"]>=100 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0
report={
  "engine":"JESSE",
  "strategyId":STRATEGY_ID,
  "status":"PASS" if passed else "FAIL",
  "pass":passed,
  "base":base,
  "stress2x":stress,
  "generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),
  "notes":"Independent Jesse research backtest on Binance Spot 1m candles aggregated by Jesse to 15m. Long-only strategy; 1x futures simulator is used only because Jesse permits bracket exits pre-entry in that mode."
}
pathlib.Path("validation/fusion/jesse-latest.json").write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
