#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import time
import subprocess
import sys
import urllib.parse
import urllib.request
from decimal import Decimal

import numpy as np
import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

VECTOR_PATH = pathlib.Path("validation/fusion/vectorbt-latest.json")
OUT = pathlib.Path("validation/fusion/nautilus-latest.json")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DAYS = 75
BASE_FEE = 0.001
STRESS_FEE = 0.002
STARTING_USDT = 20.08
TRADE_USDT = 5.5
STRATEGY_ID = "TST_NAUTILUS_EXECUTION_VALIDATOR_V1"

PRECISIONS = {
    "BTCUSDT": (2, 6),
    "ETHUSDT": (2, 6),
    "SOLUSDT": (3, 5),
}

def fetch_klines(symbol: str, days: int = DAYS) -> pd.DataFrame:
    now = int(time.time() * 1000)
    start = now - days * 24 * 60 * 60 * 1000
    rows = []
    cursor = start
    while cursor < now:
        qs = urllib.parse.urlencode({
            "symbol": symbol,
            "interval": "15m",
            "limit": 1000,
            "startTime": cursor,
            "endTime": now,
        })
        req = urllib.request.Request(
            "https://data-api.binance.vision/api/v3/klines?" + qs,
            headers={"User-Agent": "tst-nautilus-validator/1.0"},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            batch = json.load(r)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 15 * 60 * 1000
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.02)
    if len(rows) < 1500:
        raise RuntimeError(f"{symbol}: insufficient bars {len(rows)}")
    df = pd.DataFrame(rows, columns=[
        "open_time","open","high","low","close","volume","close_time","quote_volume",
        "trades","taker_base","taker_quote","ignore"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("timestamp")[["open","high","low","close","volume"]].dropna()

def ema(values, n):
    if len(values) < n:
        return None
    a = 2.0 / (n + 1)
    out = values[0]
    for x in values[1:]:
        out = a * x + (1 - a) * out
    return out

def rsi(values, n=14):
    if len(values) < n + 1:
        return 50.0
    d = np.diff(np.asarray(values[-(n + 1):], dtype=float))
    g = np.clip(d, 0, None).mean()
    l = np.clip(-d, 0, None).mean()
    if l <= 1e-15:
        return 100.0
    return 100.0 - 100.0 / (1.0 + g / l)

class FusionCandidateConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    family: str
    params_json: str
    trade_quote: float = TRADE_USDT

class FusionCandidateStrategy(Strategy):
    def __init__(self, config: FusionCandidateConfig):
        super().__init__(config)
        self.params = json.loads(config.params_json)
        self.closes = []
        self.highs = []
        self.lows = []
        self.opens = []
        self.volumes = []
        self.entry_price = None
        self.pending_entry = False
        self.pending_exit = False

    def on_start(self):
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar):
        o = bar.open.as_double()
        h = bar.high.as_double()
        l = bar.low.as_double()
        c = bar.close.as_double()
        v = bar.volume.as_double()
        self.opens.append(o); self.highs.append(h); self.lows.append(l); self.closes.append(c); self.volumes.append(v)
        if len(self.closes) > 300:
            self.opens = self.opens[-300:]; self.highs = self.highs[-300:]; self.lows = self.lows[-300:]; self.closes = self.closes[-300:]; self.volumes = self.volumes[-300:]
        if len(self.closes) < 100:
            return

        if self.portfolio.is_net_long(self.config.instrument_id):
            if self.entry_price is None:
                self.entry_price = c
            sl = float(self.params["sl"])
            tp = float(self.params["tp"])
            stop_px = self.entry_price * (1 - sl)
            target_px = self.entry_price * (1 + tp)
            stop_hit = l <= stop_px
            target_hit = h >= target_px
            signal_exit = self._exit_signal()
            if (stop_hit or target_hit or signal_exit) and not self.pending_exit:
                self.pending_exit = True
                self.close_all_positions(self.config.instrument_id)
            return

        if self.portfolio.is_flat(self.config.instrument_id) and not self.pending_entry and self._entry_signal():
            instrument = self.cache.instrument(self.config.instrument_id)
            qty = max(10 ** (-instrument.size_precision), self.config.trade_quote / c)
            order = self.order_factory.market(
                self.config.instrument_id,
                OrderSide.BUY,
                instrument.make_qty(Decimal(str(qty))),
            )
            self.pending_entry = True
            self.submit_order(order)

    def _entry_signal(self):
        p = self.params
        c = self.closes[-1]
        r = rsi(self.closes)
        vols = self.volumes[-24:]
        med = float(np.median(vols[:-1])) if len(vols) > 1 else 0.0
        rel = self.volumes[-1] / med if med > 0 else 0.0
        fam = self.config.family

        if fam == "TREND_BREAKOUT":
            f = ema(self.closes[-max(p["slow"] * 3, 120):], int(p["fast"]))
            s = ema(self.closes[-max(p["slow"] * 3, 120):], int(p["slow"]))
            hh = max(self.highs[-int(p["lookback"]) - 1:-1])
            return bool(f and s and f > s and c > hh and rel >= p["relvol"] and 52 <= r <= 72)

        if fam == "MEAN_REVERSION":
            x = np.asarray(self.closes[-20:], dtype=float)
            mid = float(x.mean()); sd = float(x.std())
            lower = mid - float(p["bb"]) * sd
            return c < lower and r <= p["rsi_in"] and rel >= 0.75

        lookback = int(p["lookback"])
        hh = max(self.highs[-lookback - 1:-1])
        e20 = ema(self.closes[-80:], 20)
        return bool(e20 and c > hh and rel >= p["relvol"] and p["rsi_min"] <= r <= 74 and c > e20)

    def _exit_signal(self):
        p = self.params
        c = self.closes[-1]
        r = rsi(self.closes)
        fam = self.config.family
        if fam == "TREND_BREAKOUT":
            f = ema(self.closes[-max(p["slow"] * 3, 120):], int(p["fast"]))
            s = ema(self.closes[-max(p["slow"] * 3, 120):], int(p["slow"]))
            return bool(f and s and (f < s or r < 45))
        if fam == "MEAN_REVERSION":
            x = np.asarray(self.closes[-20:], dtype=float)
            return c >= float(x.mean()) or r >= p["rsi_out"]
        e20 = ema(self.closes[-80:], 20)
        return bool(e20 and (c < e20 or r < 45))

    def on_position_opened(self, event):
        self.pending_entry = False
        self.pending_exit = False
        self.entry_price = float(event.avg_px_open)

    def on_position_closed(self, event):
        self.pending_entry = False
        self.pending_exit = False
        self.entry_price = None

    def on_order_rejected(self, event):
        self.pending_entry = False
        self.pending_exit = False

    def on_stop(self):
        if self.portfolio.is_net_long(self.config.instrument_id):
            self.close_all_positions(self.config.instrument_id)

def make_instrument(symbol: str, fee: float) -> CurrencyPair:
    pp, sp = PRECISIONS[symbol]
    base = symbol[:-4]
    return CurrencyPair(
        instrument_id=InstrumentId.from_str(f"{symbol}.BINANCE"),
        raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(base),
        quote_currency=Currency.from_str("USDT"),
        price_precision=pp,
        size_precision=sp,
        price_increment=Price.from_str(f"{10 ** (-pp):.{pp}f}"),
        size_increment=Quantity.from_str(f"{10 ** (-sp):.{sp}f}"),
        maker_fee=Decimal(str(fee)),
        taker_fee=Decimal(str(fee)),
        ts_event=0,
        ts_init=0,
    )

def parse_money(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(x).replace(",", ""))
    return float(m.group(0)) if m else 0.0

def run_symbol(symbol: str, df: pd.DataFrame, family: str, params: dict, fee: float):
    instrument = make_instrument(symbol, fee)
    bar_type = BarType.from_str(f"{symbol}.BINANCE-15-MINUTE-LAST-EXTERNAL")
    bars = BarDataWrangler(bar_type=bar_type, instrument=instrument).process(df)

    engine = BacktestEngine(config=BacktestEngineConfig())
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=None,
        starting_balances=[Money.from_str(f"{STARTING_USDT} USDT")],
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)
    engine.add_strategy(FusionCandidateStrategy(FusionCandidateConfig(
        instrument_id=instrument.id,
        bar_type=bar_type,
        family=family,
        params_json=json.dumps(params),
        trade_quote=TRADE_USDT,
    )))
    engine.run()

    rep = engine.generate_positions_report()
    out = []
    if rep is not None and not rep.empty:
        if "ts_closed" in rep.columns:
            rep = rep[rep["ts_closed"].notna()]
        for _, row in rep.iterrows():
            out.append({
                "exit": str(row.get("ts_closed", "")),
                "pnl": parse_money(row.get("realized_pnl", 0)),
            })
    engine.dispose()
    return out

def aggregate(trades):
    trades = sorted(trades, key=lambda x: x["exit"])
    pnl = np.asarray([x["pnl"] for x in trades], dtype=float)
    n = len(pnl)
    if n == 0:
        return {"trades":0,"wins":0,"winRate":0.0,"netPnlUSDT":0.0,"expectancyUSDT":0.0,"profitFactor":0.0,"maxDrawdownUSDT":0.0}
    gp = float(pnl[pnl > 0].sum()) if np.any(pnl > 0) else 0.0
    gl = float(-pnl[pnl < 0].sum()) if np.any(pnl < 0) else 0.0
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[:-1]
    dd = peak - eq
    return {
        "trades": int(n),
        "wins": int((pnl > 0).sum()),
        "winRate": float((pnl > 0).mean()),
        "netPnlUSDT": float(pnl.sum()),
        "expectancyUSDT": float(pnl.mean()),
        "profitFactor": float(gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "maxDrawdownUSDT": float(max(0.0, dd.max(initial=0.0))),
    }

def child_main(symbol: str, fee: float, family: str, params_json: str):
    params = json.loads(params_json)
    df = fetch_klines(symbol)
    trades = run_symbol(symbol, df, family, params, fee)
    print("CHILD_RESULT=" + json.dumps(trades))

def run_child(symbol: str, fee: float, family: str, params: dict):
    cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), "--child", symbol, str(fee), family, json.dumps(params, separators=(",", ":"))]
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("CHILD_RESULT="):
            return json.loads(line.split("=", 1)[1])
    raise RuntimeError(f"child result missing for {symbol}: {proc.stdout[-1000:]}")

if len(sys.argv) >= 6 and sys.argv[1] == "--child":
    child_main(sys.argv[2], float(sys.argv[3]), sys.argv[4], sys.argv[5])
    raise SystemExit(0)

vector = json.loads(VECTOR_PATH.read_text())
selected = vector.get("selected")
if not selected:
    report = {
        "engine":"NAUTILUS_TRADER",
        "strategyId":STRATEGY_ID,
        "status":"NO_VECTORBT_CANDIDATE",
        "pass":False,
        "authorization":"RESEARCH_ONLY",
        "liveTrading":False,
        "candidateId":None,
        "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
    }
else:
    family = selected["family"]
    params = selected["params"]
    base_trades, stress_trades = [], []
    for symbol in SYMBOLS:
        base_trades += run_child(symbol, BASE_FEE, family, params)
        stress_trades += run_child(symbol, STRESS_FEE, family, params)
    base = aggregate(base_trades)
    stress = aggregate(stress_trades)
    independent_pass = (
        base["trades"] >= 30
        and base["profitFactor"] >= 1.10
        and base["expectancyUSDT"] > 0
        and stress["profitFactor"] >= 1.0
        and stress["expectancyUSDT"] > 0
        and base["maxDrawdownUSDT"] <= 4.0
    )
    passed = bool(vector.get("pass") and independent_pass)
    report = {
        "engine":"NAUTILUS_TRADER",
        "strategyId":STRATEGY_ID,
        "status":"PASS" if passed else "FAIL",
        "pass":passed,
        "independentEnginePass":independent_pass,
        "vectorbtDiscoveryPass":bool(vector.get("pass")),
        "authorization":"RESEARCH_ONLY",
        "liveTrading":False,
        "candidateId":selected.get("candidateId"),
        "family":family,
        "params":params,
        "symbols":SYMBOLS,
        "days":DAYS,
        "base":base,
        "stress2x":stress,
        "generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes":"NautilusTrader event-driven Binance Spot CASH backtest. Each symbol runs in its own process to isolate the engine logger. Market orders, long-only, 5.5 USDT notional, no leverage."
    }

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
