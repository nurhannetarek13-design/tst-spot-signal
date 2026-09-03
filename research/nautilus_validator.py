#!/usr/bin/env python3
from __future__ import annotations
import datetime as dt, json, math, pathlib, re, time, subprocess, sys, urllib.parse, urllib.request
from decimal import Decimal
import numpy as np, pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

MANIFEST_PATH=pathlib.Path("validation/fusion/candidate-manifest.json")
OUT=pathlib.Path("validation/fusion/nautilus-latest.json")
DAYS=365
BASE_FEE=0.0015
STRESS_FEE=0.003
STARTING_USDT=20.08
TRADE_USDT=5.5
STRATEGY_ID="TST_CANDIDATE_NAUTILUS_VALIDATOR_V1"

def api_json(path):
    req=urllib.request.Request("https://data-api.binance.vision"+path,headers={"User-Agent":"tst-unified-nautilus/1.0"})
    with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)

def fetch_klines(symbol,tf):
    end=int(time.time()*1000);start=end-DAYS*86400000
    step=3600000 if tf=="1h" else 900000
    rows=[];cur=start
    while cur<end:
        qs=urllib.parse.urlencode({"symbol":symbol,"interval":tf,"limit":1000,"startTime":cur,"endTime":end})
        batch=api_json("/api/v3/klines?"+qs)
        if not batch:break
        rows.extend(batch);nxt=int(batch[-1][0])+step
        if nxt<=cur:break
        cur=nxt;time.sleep(0.01)
    if len(rows)<1000:raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time","quote_volume","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume"]:df[c]=pd.to_numeric(df[c],errors="coerce")
    df["timestamp"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
    return df.set_index("timestamp")[["open","high","low","close","volume"]].dropna()

def normalize_increment(x):
    s=str(x)
    if "." in s:
        s=s.rstrip("0").rstrip(".")
    return s or "0"

def precision_from_increment(x):
    s=normalize_increment(x)
    if "." not in s:return 0
    return len(s.split(".")[1])

def instrument_meta(symbol):
    info=api_json("/api/v3/exchangeInfo?symbol="+symbol)
    s=info["symbols"][0]
    fs={x["filterType"]:x for x in s.get("filters",[])}
    tick=normalize_increment(fs.get("PRICE_FILTER",{}).get("tickSize","0.00000001"))
    step=normalize_increment(fs.get("LOT_SIZE",{}).get("stepSize","0.00000001"))
    return {"price_precision":precision_from_increment(tick),"size_precision":precision_from_increment(step),"tick":tick,"step":step}

def ema(values,n):
    if len(values)<n:return None
    a=2/(n+1);e=float(values[0])
    for x in values[1:]:e=a*float(x)+(1-a)*e
    return e
def rsi(values,n=14):
    if len(values)<n+1:return 50.0
    d=np.diff(np.asarray(values[-(n+1):],dtype=float));g=np.clip(d,0,None).mean();l=np.clip(-d,0,None).mean()
    return 100.0 if l<=1e-15 else 100-100/(1+g/l)
def atr_pct(highs,lows,closes,n=14):
    if len(closes)<n+1:return 0.0
    vals=[]
    for i in range(len(closes)-n,len(closes)):
        p=closes[i-1];vals.append(max(highs[i]-lows[i],abs(highs[i]-p),abs(lows[i]-p)))
    return (sum(vals)/len(vals))/closes[-1] if closes[-1]>0 else 0.0

class UnifiedConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    family: str
    params_json: str
    trade_quote: float=TRADE_USDT

class UnifiedStrategy(Strategy):
    def __init__(self,config:UnifiedConfig):
        super().__init__(config);self.p=json.loads(config.params_json)
        self.opens=[];self.highs=[];self.lows=[];self.closes=[];self.volumes=[]
        self.entry_price=None;self.pending_entry=False;self.pending_exit=False;self.bars_in_position=0
    def on_start(self):self.subscribe_bars(self.config.bar_type)
    def on_bar(self,bar:Bar):
        o=bar.open.as_double();h=bar.high.as_double();l=bar.low.as_double();c=bar.close.as_double();v=bar.volume.as_double()
        self.opens.append(o);self.highs.append(h);self.lows.append(l);self.closes.append(c);self.volumes.append(v)
        if len(self.closes)>1000:
            self.opens=self.opens[-1000:];self.highs=self.highs[-1000:];self.lows=self.lows[-1000:];self.closes=self.closes[-1000:];self.volumes=self.volumes[-1000:]
        if len(self.closes)<160:return

        if self.portfolio.is_net_long(self.config.instrument_id):
            self.bars_in_position+=1
            if self.entry_price is None:self.entry_price=c
            sl=float(self.p.get("sl",0.03));tp=float(self.p.get("tp",0.06));hold=int(self.p.get("holdBars",0))
            stop=self.entry_price*(1-sl);target=self.entry_price*(1+tp)
            if (l<=stop or h>=target or (hold>0 and self.bars_in_position>=hold) or self._exit_signal()) and not self.pending_exit:
                self.pending_exit=True;self.close_all_positions(self.config.instrument_id)
            return

        if self.portfolio.is_flat(self.config.instrument_id) and not self.pending_entry and self._entry_signal():
            ins=self.cache.instrument(self.config.instrument_id);qty=max(10**(-ins.size_precision),self.config.trade_quote/c)
            order=self.order_factory.market(self.config.instrument_id,OrderSide.BUY,ins.make_qty(Decimal(str(qty))))
            self.pending_entry=True;self.submit_order(order)

    def _features(self):
        c=np.asarray(self.closes,dtype=float);v=np.asarray(self.volumes,dtype=float);qv=v*c
        med=float(np.median(qv[-25:-1])) if len(qv)>=25 else 0
        rel=float(qv[-1]/med) if med>0 else 0
        return c,qv,rel,rsi(c),atr_pct(self.highs,self.lows,self.closes)

    def _entry_signal(self):
        p=self.p;fam=self.config.family;c,qv,rel,R,a=self._features();close=float(c[-1])
        if fam=="TS_MOMENTUM":
            ef=ema(c[-400:],int(p.get("emaFast",48)));es=ema(c[-500:],int(p.get("emaSlow",120)));lb=int(p.get("retLookback",24));ret=close/c[-lb-1]-1
            return bool(ef and es and close>ef>es and ret>float(p.get("retMin",0.02)) and float(p.get("atrMin",0.006))<=a<=float(p.get("atrMax",0.08)) and rel>=float(p.get("relvol",0.8)))
        if fam=="LIQUIDITY_REVERSAL":
            lb=int(p.get("retLookback",6));zlb=int(p.get("zLookback",720))
            if len(c)<zlb+lb+2:return False
            rr=np.asarray([c[i]/c[i-lb]-1 for i in range(len(c)-zlb,len(c))]);sd=float(rr.std());z=((close/c[-lb-1]-1)-float(rr.mean()))/sd if sd>0 else 0
            week=qv[-7*24:];vr=float(qv[-1]/np.median(week)) if len(week) and np.median(week)>0 else 999;e24=ema(c[-100:],24)
            return bool(z<=float(p.get("zMax",-2)) and vr<=float(p.get("volumeRatioMax",1.1)) and R<=float(p.get("rsiMax",35)) and e24 and close<e24)
        if fam=="VOLATILITY_BREAKOUT":
            lb=int(p.get("lookback",24));hh=max(self.highs[-lb-1:-1]);clb=int(p.get("compressionLookback",72))
            samples=[]
            for off in range(clb,0,-1):
                if len(self.closes)-off>20:samples.append(atr_pct(self.highs[:-off],self.lows[:-off],self.closes[:-off]))
            pct=sum(1 for x in samples if x<=a)/len(samples) if samples else 1
            return bool(pct<=float(p.get("compressionPct",0.25)) and close>hh and rel>=float(p.get("relvol",1.5)) and float(p.get("rsiMin",55))<=R<=float(p.get("rsiMax",75)))
        if fam=="TREND_BREAKOUT":
            ef=ema(c[-300:],int(p.get("fast",20)));es=ema(c[-300:],int(p.get("slow",60)));hh=max(self.highs[-int(p.get("lookback",20))-1:-1])
            return bool(ef and es and ef>es and close>hh and rel>=float(p.get("relvol",1.0)) and 52<=R<=72)
        if fam=="MEAN_REVERSION":
            x=c[-20:];mid=float(x.mean());sd=float(x.std());lower=mid-float(p.get("bb",2))*sd
            return bool(close<lower and R<=float(p.get("rsi_in",34)) and rel>=0.75)
        if fam=="VOLATILITY_MOMENTUM":
            hh=max(self.highs[-int(p.get("lookback",20))-1:-1]);e20=ema(c[-100:],20)
            return bool(e20 and close>hh and rel>=float(p.get("relvol",1.4)) and float(p.get("rsi_min",52))<=R<=74 and close>e20)
        return False

    def _exit_signal(self):
        fam=self.config.family;p=self.p;c=np.asarray(self.closes,dtype=float);R=rsi(c);close=float(c[-1])
        if fam=="MEAN_REVERSION":return close>=float(c[-20:].mean()) or R>=float(p.get("rsi_out",50))
        if fam=="TREND_BREAKOUT":
            ef=ema(c[-300:],int(p.get("fast",20)));es=ema(c[-300:],int(p.get("slow",60)));return bool(ef and es and (ef<es or R<45))
        if fam=="VOLATILITY_MOMENTUM":
            e20=ema(c[-100:],20);return bool(e20 and (close<e20 or R<45))
        return False

    def on_position_opened(self,event):
        self.pending_entry=False;self.pending_exit=False;self.entry_price=float(event.avg_px_open);self.bars_in_position=0
    def on_position_closed(self,event):
        self.pending_entry=False;self.pending_exit=False;self.entry_price=None;self.bars_in_position=0
    def on_order_rejected(self,event):
        self.pending_entry=False;self.pending_exit=False
    def on_stop(self):
        if self.portfolio.is_net_long(self.config.instrument_id):self.close_all_positions(self.config.instrument_id)

def make_instrument(symbol,fee,meta):
    return CurrencyPair(
        instrument_id=InstrumentId.from_str(f"{symbol}.BINANCE"),raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(symbol[:-4]),quote_currency=Currency.from_str("USDT"),
        price_precision=meta["price_precision"],size_precision=meta["size_precision"],
        price_increment=Price.from_str(meta["tick"]),size_increment=Quantity.from_str(meta["step"]),
        maker_fee=Decimal(str(fee)),taker_fee=Decimal(str(fee)),ts_event=0,ts_init=0)

def parse_money(x):
    if x is None or (isinstance(x,float) and math.isnan(x)):return 0.0
    m=re.search(r"[-+]?\d+(?:\.\d+)?",str(x).replace(",",""));return float(m.group(0)) if m else 0.0

def run_once(symbol,tf,family,params,fee):
    df=fetch_klines(symbol,tf);meta=instrument_meta(symbol);ins=make_instrument(symbol,fee,meta)
    bar_spec="1-HOUR" if tf=="1h" else "15-MINUTE"
    bt=BarType.from_str(f"{symbol}.BINANCE-{bar_spec}-LAST-EXTERNAL")
    bars=BarDataWrangler(bar_type=bt,instrument=ins).process(df)
    engine=BacktestEngine(config=BacktestEngineConfig())
    engine.add_venue(venue=Venue("BINANCE"),oms_type=OmsType.NETTING,account_type=AccountType.CASH,base_currency=None,starting_balances=[Money.from_str(f"{STARTING_USDT} USDT")])
    engine.add_instrument(ins);engine.add_data(bars);engine.add_strategy(UnifiedStrategy(UnifiedConfig(instrument_id=ins.id,bar_type=bt,family=family,params_json=json.dumps(params),trade_quote=TRADE_USDT)));engine.run()
    rep=engine.trader.generate_positions_report();out=[]
    if rep is not None and not rep.empty:
        if "ts_closed" in rep.columns:rep=rep[rep["ts_closed"].notna()]
        for _,row in rep.iterrows():out.append({"exit":str(row.get("ts_closed","")),"pnl":parse_money(row.get("realized_pnl",0))})
    engine.dispose();return out

def child(symbol,tf,fee,family,params_json):
    print("CHILD_RESULT="+json.dumps(run_once(symbol,tf,family,json.loads(params_json),fee)))

def run_child(symbol,tf,fee,family,params):
    cmd=[sys.executable,str(pathlib.Path(__file__).resolve()),"--child",symbol,tf,str(fee),family,json.dumps(params,separators=(",",":"))]
    p=subprocess.run(cmd,text=True,capture_output=True)
    if p.returncode!=0:raise RuntimeError(p.stderr[-5000:]+"\n"+p.stdout[-1000:])
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("CHILD_RESULT="):return json.loads(line.split("=",1)[1])
    raise RuntimeError("child result missing")

def metrics(trades):
    p=np.asarray([x["pnl"] for x in sorted(trades,key=lambda z:z["exit"])],dtype=float)
    if len(p)==0:return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    gp=float(p[p>0].sum()) if np.any(p>0) else 0;gl=float(-p[p<0].sum()) if np.any(p<0) else 0;eq=np.cumsum(p);peak=np.maximum.accumulate(np.r_[0.0,eq])[:-1]
    return {"trades":int(len(p)),"wins":int((p>0).sum()),"winRate":float((p>0).mean()),"netPnlUSDT":float(p.sum()),"expectancyUSDT":float(p.mean()),"profitFactor":float(gp/gl) if gl>0 else (999.0 if gp>0 else 0.0),"maxDrawdownUSDT":float(max(0.0,(peak-eq).max(initial=0.0)))}

if len(sys.argv)>=7 and sys.argv[1]=="--child":
    child(sys.argv[2],sys.argv[3],float(sys.argv[4]),sys.argv[5],sys.argv[6]);raise SystemExit(0)

m=json.loads(MANIFEST_PATH.read_text())
if not m.get("candidateFingerprint"):
    report={"engine":"NAUTILUS_TRADER","strategyId":STRATEGY_ID,"status":"NO_CANDIDATE","pass":False,"candidateFingerprint":None,"liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat()}
else:
    base=metrics(run_child(m["symbol"],m["timeframe"],BASE_FEE,m["family"],m["params"]))
    stress=metrics(run_child(m["symbol"],m["timeframe"],STRESS_FEE,m["family"],m["params"]))
    independent=base["trades"]>=30 and base["profitFactor"]>=1.15 and base["expectancyUSDT"]>0 and stress["profitFactor"]>=1.0 and stress["expectancyUSDT"]>0
    passed=independent and base["trades"]>=40
    report={"engine":"NAUTILUS_TRADER","strategyId":STRATEGY_ID,"status":"PASS" if passed else "FAIL","pass":passed,"independentEnginePass":independent,"candidateId":m["candidateId"],"candidateFingerprint":m["candidateFingerprint"],"symbol":m["symbol"],"family":m["family"],"timeframe":m["timeframe"],"params":m["params"],"base":base,"stress2x":stress,"authorization":"RESEARCH_ONLY","liveTrading":False,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),"notes":"NautilusTrader event-driven Binance Spot CASH validation of the exact unified candidate; long-only; no leverage."}
OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
