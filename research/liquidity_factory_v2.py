#!/usr/bin/env python3
"""Liquidity Factory V2 for Binance Spot research.

Idea under test (long-only):
4H bullish regime -> 1H discount -> 15m sell-side liquidity sweep/reclaim ->
internal CHoCH -> participation confirmation -> next-bar entry.

Research contract:
- Public Binance Vision Spot 15m archives only. No account/API keys/order endpoints.
- Spot long-only, no leverage, fixed 10 USDT notional per trade.
- Signals use completed candles; discretionary entries execute at next bar open.
- Protective stop/target may trigger intrabar; if both touch, stop wins.
- Base costs: 0.10% fee/side + 0.05% slippage/side.
- Discovery 60% -> Validation 20% -> frozen finalists -> untouched OOS 20%.
- OOS is never used for selection/tuning.
- Finalists also face a harsher OOS cost stress.
- Output can only be REJECT or RESEARCH_PASS_NOT_LIVE.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import math
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

AUTHORIZATION = "RESEARCH_ONLY"
BASE = "https://data.binance.vision/data/spot/monthly/klines"
UA = "tst-liquidity-factory-v2/1.0"
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.0010
    slippage_rate: float = 0.0005


@dataclass(frozen=True)
class RiskModel:
    trade_usdt: float = 10.0
    stop_atr_buffer: float = 0.25
    reward_r: float = 2.0
    max_bars: int = 96
    confirm_window: int = 6


@dataclass(frozen=True)
class Candidate:
    htf_regime: str
    discount: str
    sweep_lookback: int
    sweep_depth_atr: float
    choch_lookback: int
    participation: str

    @property
    def id(self) -> str:
        return (
            f"{self.htf_regime}__{self.discount}__sweep{self.sweep_lookback}"
            f"__depth{self.sweep_depth_atr:.2f}__choch{self.choch_lookback}__{self.participation}"
        )


@dataclass
class Trade:
    symbol: str
    entry_ts: str
    exit_ts: str
    entry: float
    exit: float
    qty: float
    pnl_usdt: float
    return_pct: float
    bars_held: int
    reason: str


HTF_REGIMES = ["ema_strict", "ema_slope"]
DISCOUNTS = ["range24", "range48"]
SWEEP_LOOKBACKS = [16, 32]
SWEEP_DEPTHS = [0.05, 0.15]
CHOCH_LOOKBACKS = [4, 8]
PARTICIPATION = ["taker53", "relvol115", "taker_and_relvol"]


def candidate_library() -> list[Candidate]:
    return [
        Candidate(*x)
        for x in itertools.product(
            HTF_REGIMES,
            DISCOUNTS,
            SWEEP_LOOKBACKS,
            SWEEP_DEPTHS,
            CHOCH_LOOKBACKS,
            PARTICIPATION,
        )
    ]


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def fetch_verified_zip(url: str) -> bytes:
    raw = http_bytes(url)
    expected = http_bytes(url + ".CHECKSUM").decode(errors="replace").strip().split()[0].lower()
    actual = hashlib.sha256(raw).hexdigest()
    if len(expected) != 64 or actual != expected:
        raise RuntimeError(f"checksum mismatch for {url}")
    return raw


def parse_epoch(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    med = float(numeric.dropna().median()) if numeric.notna().any() else 0.0
    unit = "ns" if med > 1e17 else ("us" if med > 1e14 else "ms")
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")


def read_kline_zip(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(csvs) != 1:
            raise RuntimeError(f"expected one CSV, got {csvs}")
        with zf.open(csvs[0]) as f:
            df = pd.read_csv(f, header=None)
    if len(df) and not str(df.iloc[0, 0]).replace("-", "").isdigit():
        df = df.iloc[1:].reset_index(drop=True)
    if df.shape[1] < len(KLINE_COLS):
        raise RuntimeError(f"unexpected columns={df.shape[1]}")
    df = df.iloc[:, : len(KLINE_COLS)].copy()
    df.columns = KLINE_COLS
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = parse_epoch(df["open_time"])
    return df.dropna(subset=["ts", "open", "high", "low", "close"])


def month_stamps(start: str, end: str) -> list[str]:
    a = pd.Timestamp(start).to_period("M")
    b = (pd.Timestamp(end) - pd.Timedelta(microseconds=1)).to_period("M")
    return [str(p) for p in pd.period_range(a, b, freq="M")]


def fetch_spot_klines(symbol: str, start: str, end: str, min_coverage: float = 0.95):
    parts = []
    sources = []
    for stamp in month_stamps(start, end):
        url = f"{BASE}/{symbol}/15m/{symbol}-15m-{stamp}.zip"
        try:
            raw = fetch_verified_zip(url)
            frame = read_kline_zip(raw)
            parts.append(frame)
            sources.append({"month": stamp, "status": "OK", "rows": len(frame), "checksumVerified": True})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                sources.append({"month": stamp, "status": "MISSING_404", "rows": 0, "checksumVerified": False})
                continue
            raise
        time.sleep(0.01)
    if not parts:
        raise RuntimeError(f"No Binance Vision data for {symbol}")
    df = pd.concat(parts, ignore_index=True).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    df = df[(df["ts"] >= start_ts) & (df["ts"] < end_ts)].copy().reset_index(drop=True)
    expected = max(1, int((end_ts - start_ts) / pd.Timedelta(minutes=15)))
    coverage = len(df) / expected
    if coverage < min_coverage:
        missing = [x["month"] for x in sources if x["status"] != "OK"]
        raise RuntimeError(f"coverage={coverage:.3%} < {min_coverage:.0%}; missing={missing}")
    return df, sources, coverage


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)


def make_htf(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = df.copy()
    x["bar_end"] = x["ts"] + pd.Timedelta(minutes=15)
    x = x.set_index("bar_end")
    ohlc = x.resample(rule, label="right", closed="right").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last")
    ).dropna()
    return ohlc


def add_features(df: pd.DataFrame, btc_4h_regime: pd.DataFrame | None, symbol: str) -> pd.DataFrame:
    d = df.copy()
    d["bar_end"] = d["ts"] + pd.Timedelta(minutes=15)
    tr = true_range(d)
    d["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["relvol20"] = d["quote_volume"] / d["quote_volume"].rolling(20, min_periods=20).mean().replace(0, np.nan)
    d["taker_share"] = d["taker_buy_quote"] / d["quote_volume"].replace(0, np.nan)

    h1 = make_htf(df, "1h")
    h1["range24_high"] = h1["high"].rolling(24, min_periods=24).max()
    h1["range24_low"] = h1["low"].rolling(24, min_periods=24).min()
    h1["range48_high"] = h1["high"].rolling(48, min_periods=48).max()
    h1["range48_low"] = h1["low"].rolling(48, min_periods=48).min()
    h1["discount24"] = h1["close"] <= (h1["range24_high"] + h1["range24_low"]) / 2.0
    h1["discount48"] = h1["close"] <= (h1["range48_high"] + h1["range48_low"]) / 2.0

    h4 = make_htf(df, "4h")
    h4["ema50"] = h4["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    h4["ema200"] = h4["close"].ewm(span=200, adjust=False, min_periods=200).mean()
    h4["regime_strict"] = (h4["close"] > h4["ema50"]) & (h4["ema50"] > h4["ema200"])
    h4["regime_slope"] = (h4["close"] > h4["ema50"]) & (h4["ema50"] > h4["ema50"].shift(3))

    d = pd.merge_asof(
        d.sort_values("bar_end"),
        h1[["discount24", "discount48"]].reset_index().sort_values("bar_end"),
        on="bar_end",
        direction="backward",
    )
    d = pd.merge_asof(
        d.sort_values("bar_end"),
        h4[["regime_strict", "regime_slope"]].reset_index().sort_values("bar_end"),
        on="bar_end",
        direction="backward",
    )

    if symbol == "BTCUSDT":
        d["btc_regime"] = True
    else:
        if btc_4h_regime is None:
            raise RuntimeError("BTC 4H regime frame required for alt symbols")
        d = pd.merge_asof(
            d.sort_values("bar_end"),
            btc_4h_regime.reset_index().sort_values("bar_end"),
            on="bar_end",
            direction="backward",
        )
        d["btc_regime"] = d["btc_regime"].fillna(False)

    return d.reset_index(drop=True)


def build_btc_regime_frame(btc_df: pd.DataFrame) -> pd.DataFrame:
    h4 = make_htf(btc_df, "4h")
    h4["ema50"] = h4["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    h4["ema200"] = h4["close"].ewm(span=200, adjust=False, min_periods=200).mean()
    h4["btc_regime"] = (h4["close"] > h4["ema50"]) & (h4["ema50"] > h4["ema200"])
    return h4[["btc_regime"]]


def participation_ok(row_idx: int, arrays: dict[str, np.ndarray], mode: str) -> bool:
    taker = arrays["taker_share"][row_idx]
    relv = arrays["relvol20"][row_idx]
    if mode == "taker53":
        return math.isfinite(taker) and taker >= 0.53
    if mode == "relvol115":
        return math.isfinite(relv) and relv >= 1.15
    if mode == "taker_and_relvol":
        return math.isfinite(taker) and math.isfinite(relv) and taker >= 0.53 and relv >= 1.10
    raise KeyError(mode)


def arrays_from(d: pd.DataFrame) -> dict[str, np.ndarray]:
    keys = [
        "open", "high", "low", "close", "atr14", "relvol20", "taker_share",
        "discount24", "discount48", "regime_strict", "regime_slope", "btc_regime",
    ]
    out = {k: d[k].to_numpy() for k in keys}
    out["ts"] = d["ts"].astype(str).to_numpy()
    return out


def simulate(symbol: str, d: pd.DataFrame, c: Candidate, cost: CostModel, risk: RiskModel) -> list[Trade]:
    a = arrays_from(d)
    n = len(d)
    trades: list[Trade] = []
    position = None
    setup = None
    pending_entry = None

    regime_key = "regime_strict" if c.htf_regime == "ema_strict" else "regime_slope"
    discount_key = "discount24" if c.discount == "range24" else "discount48"

    lows = pd.Series(a["low"])
    highs = pd.Series(a["high"])
    prior_liq = lows.shift(1).rolling(c.sweep_lookback, min_periods=c.sweep_lookback).min().to_numpy()
    prior_choch = highs.shift(1).rolling(c.choch_lookback, min_periods=c.choch_lookback).max().to_numpy()

    warmup = max(900, c.sweep_lookback + c.choch_lookback + 5)
    for i in range(warmup, n):
        # Execute confirmed entry at next bar open.
        if pending_entry is not None and position is None:
            entry_px = float(a["open"][i]) * (1.0 + cost.slippage_rate)
            stop = float(pending_entry["stop"])
            if entry_px > stop > 0:
                risk_dist = entry_px - stop
                target = entry_px + risk.reward_r * risk_dist
                qty = risk.trade_usdt / entry_px
                position = {
                    "entry": entry_px,
                    "stop": stop,
                    "target": target,
                    "qty": qty,
                    "entry_i": i,
                    "entry_ts": a["ts"][i],
                }
            pending_entry = None

        if position is not None:
            low = float(a["low"][i])
            high = float(a["high"][i])
            entry = position["entry"]
            qty = position["qty"]
            reason = None
            exit_px = None

            if low <= position["stop"]:
                exit_px = position["stop"] * (1.0 - cost.slippage_rate)
                reason = "stop"
            elif high >= position["target"]:
                exit_px = position["target"]
                reason = "target"
            elif i - position["entry_i"] >= risk.max_bars:
                exit_px = float(a["close"][i]) * (1.0 - cost.slippage_rate)
                reason = "timeout"

            if exit_px is not None:
                gross = qty * (exit_px - entry)
                fees = cost.fee_rate * qty * (entry + exit_px)
                pnl = gross - fees
                trades.append(
                    Trade(
                        symbol=symbol,
                        entry_ts=str(position["entry_ts"]),
                        exit_ts=str(a["ts"][i]),
                        entry=entry,
                        exit=exit_px,
                        qty=qty,
                        pnl_usdt=pnl,
                        return_pct=pnl / (qty * entry) * 100.0,
                        bars_held=i - position["entry_i"],
                        reason=reason,
                    )
                )
                position = None
            continue

        # Existing sweep setup waits for CHoCH confirmation, then next-bar entry.
        if setup is not None:
            expired = i - setup["sweep_i"] > risk.confirm_window
            invalidated = float(a["low"][i]) < setup["sweep_low"] - 0.25 * setup["atr"]
            if expired or invalidated:
                setup = None
            else:
                choch = float(a["close"][i]) > setup["choch_level"]
                regime_ok = bool(a[regime_key][i]) and bool(a["btc_regime"][i])
                if choch and regime_ok and participation_ok(i, a, c.participation):
                    stop = setup["sweep_low"] - risk.stop_atr_buffer * setup["atr"]
                    if i + 1 < n:
                        pending_entry = {"stop": stop}
                    setup = None
            continue

        atr = float(a["atr14"][i])
        liq = float(prior_liq[i]) if math.isfinite(prior_liq[i]) else math.nan
        choch_level = float(prior_choch[i]) if math.isfinite(prior_choch[i]) else math.nan
        if not (math.isfinite(atr) and atr > 0 and math.isfinite(liq) and math.isfinite(choch_level)):
            continue

        regime_ok = bool(a[regime_key][i]) and bool(a["btc_regime"][i])
        discount_ok = bool(a[discount_key][i])
        depth = c.sweep_depth_atr * atr
        sweep = float(a["low"][i]) < (liq - depth) and float(a["close"][i]) > liq

        if regime_ok and discount_ok and sweep:
            setup = {
                "sweep_i": i,
                "sweep_low": float(a["low"][i]),
                "atr": atr,
                "choch_level": choch_level,
            }

    return trades


def metrics(trades: list[Trade], initial_cash: float = 100.0) -> dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "winRatePct": 0.0,
            "netPnlUsdt": 0.0, "avgTradePct": 0.0, "medianTradePct": 0.0,
            "profitFactor": 0.0, "maxDrawdownPct": 0.0,
        }
    pnls = np.array([t.pnl_usdt for t in trades], dtype=float)
    rets = np.array([t.return_pct for t in trades], dtype=float)
    gp = float(pnls[pnls > 0].sum()) if np.any(pnls > 0) else 0.0
    gl = float(abs(pnls[pnls < 0].sum())) if np.any(pnls < 0) else 0.0
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    equity = initial_cash + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[initial_cash], equity]))[1:]
    dd_pct = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)
    return {
        "trades": int(len(trades)),
        "wins": int(np.sum(pnls > 0)),
        "losses": int(np.sum(pnls < 0)),
        "winRatePct": float(np.mean(pnls > 0) * 100.0),
        "netPnlUsdt": float(pnls.sum()),
        "avgTradePct": float(np.mean(rets)),
        "medianTradePct": float(np.median(rets)),
        "profitFactor": float(pf),
        "maxDrawdownPct": float(np.max(dd_pct)) if len(dd_pct) else 0.0,
    }


def split_frames(d: pd.DataFrame):
    n = len(d)
    i1 = int(n * 0.60)
    i2 = int(n * 0.80)
    # Preserve enough pre-segment history for indicators/liquidity context, but only score trades born inside segment.
    return d.iloc[:i1].copy(), d.iloc[max(0, i1 - 1000):i2].copy(), d.iloc[max(0, i2 - 1000):].copy(), i1, i2


def filter_segment_trades(trades: list[Trade], min_ts: pd.Timestamp) -> list[Trade]:
    cutoff = min_ts.tz_convert("UTC") if min_ts.tzinfo else min_ts.tz_localize("UTC")
    out = []
    for t in trades:
        ts = pd.Timestamp(t.entry_ts)
        if ts >= cutoff:
            out.append(t)
    return out


def evaluate_candidate(c: Candidate, frames: dict[str, pd.DataFrame], segment: str, cost: CostModel, risk: RiskModel) -> dict:
    all_trades: list[Trade] = []
    per_symbol = {}
    positive_symbols = 0
    for symbol, d in frames.items():
        disc, val, oos, i1, i2 = split_frames(d)
        if segment == "discovery":
            sub = disc
            min_ts = sub["ts"].iloc[0]
        elif segment == "validation":
            sub = val
            min_ts = d["ts"].iloc[i1]
        elif segment == "oos":
            sub = oos
            min_ts = d["ts"].iloc[i2]
        else:
            raise KeyError(segment)
        tr = filter_segment_trades(simulate(symbol, sub, c, cost, risk), pd.Timestamp(min_ts))
        m = metrics(tr)
        per_symbol[symbol] = m
        all_trades.extend(tr)
        if m["netPnlUsdt"] > 0 and m["trades"] >= 3:
            positive_symbols += 1
    agg = metrics(all_trades)
    agg["positiveSymbols"] = positive_symbols
    return {"aggregate": agg, "perSymbol": per_symbol}


def discovery_pass(m: dict) -> bool:
    a = m["aggregate"]
    return (
        a["trades"] >= 80
        and a["profitFactor"] >= 1.15
        and a["avgTradePct"] > 0
        and a["medianTradePct"] > -0.05
        and a["maxDrawdownPct"] <= 5.0
        and a["positiveSymbols"] >= 3
    )


def validation_pass(m: dict) -> bool:
    a = m["aggregate"]
    return (
        a["trades"] >= 25
        and a["profitFactor"] >= 1.10
        and a["avgTradePct"] > 0
        and a["maxDrawdownPct"] <= 4.0
        and a["positiveSymbols"] >= 3
    )


def oos_pass(base: dict, stress: dict) -> bool:
    a = base["aggregate"]
    s = stress["aggregate"]
    return (
        a["trades"] >= 25
        and a["profitFactor"] >= 1.15
        and a["avgTradePct"] > 0
        and a["maxDrawdownPct"] <= 3.0
        and a["positiveSymbols"] >= 3
        and s["profitFactor"] >= 1.00
        and s["avgTradePct"] > 0
    )


def score_for_freeze(disc: dict, val: dict) -> float:
    # Uses only Discovery + Validation. OOS remains untouched.
    da, va = disc["aggregate"], val["aggregate"]
    return (
        min(da["profitFactor"], 4.0)
        + 2.0 * min(va["profitFactor"], 4.0)
        + 5.0 * max(0.0, va["avgTradePct"])
        + 0.15 * va["positiveSymbols"]
        - 0.10 * va["maxDrawdownPct"]
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"])
    ap.add_argument("--start", default="2024-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--top-discovery", type=int, default=20)
    ap.add_argument("--top-validation", type=int, default=8)
    ap.add_argument("--output", default="artifacts/liquidity-factory-v2/report.json")
    args = ap.parse_args()

    base_cost = CostModel()
    stress_cost = CostModel(fee_rate=0.0015, slippage_rate=0.0010)
    risk = RiskModel()

    raw = {}
    data_meta = {}
    for symbol in args.symbols:
        print(f"Loading {symbol} from Binance Vision...")
        df, sources, coverage = fetch_spot_klines(symbol, args.start, args.end)
        raw[symbol] = df
        data_meta[symbol] = {"rows": len(df), "coverage": coverage, "sources": sources}

    if "BTCUSDT" not in raw:
        raise RuntimeError("BTCUSDT is mandatory for BTC regime filter")
    btc_regime = build_btc_regime_frame(raw["BTCUSDT"])
    frames = {
        symbol: add_features(df, btc_regime if symbol != "BTCUSDT" else None, symbol)
        for symbol, df in raw.items()
    }

    library = candidate_library()
    print(f"Candidate count: {len(library)}")
    discovery_results = []
    for idx, c in enumerate(library, 1):
        r = evaluate_candidate(c, frames, "discovery", base_cost, risk)
        discovery_results.append({"candidate": c, "result": r, "pass": discovery_pass(r)})
        if idx % 24 == 0:
            print(f"Discovery {idx}/{len(library)}")

    eligible = [x for x in discovery_results if x["pass"]]
    eligible.sort(
        key=lambda x: (
            x["result"]["aggregate"]["profitFactor"],
            x["result"]["aggregate"]["avgTradePct"],
            x["result"]["aggregate"]["trades"],
        ),
        reverse=True,
    )
    discovery_shortlist = eligible[: args.top_discovery]

    validation_rows = []
    for x in discovery_shortlist:
        c = x["candidate"]
        vr = evaluate_candidate(c, frames, "validation", base_cost, risk)
        validation_rows.append({
            "candidate": c,
            "discovery": x["result"],
            "validation": vr,
            "pass": validation_pass(vr),
            "freezeScore": score_for_freeze(x["result"], vr),
        })

    passed_validation = [x for x in validation_rows if x["pass"]]
    passed_validation.sort(key=lambda x: x["freezeScore"], reverse=True)
    frozen = passed_validation[: args.top_validation]

    oos_rows = []
    for x in frozen:
        c = x["candidate"]
        base = evaluate_candidate(c, frames, "oos", base_cost, risk)
        stress = evaluate_candidate(c, frames, "oos", stress_cost, risk)
        oos_rows.append({
            "candidate": c,
            "base": base,
            "stress": stress,
            "pass": oos_pass(base, stress),
        })

    passing = [x for x in oos_rows if x["pass"]]
    decision = "RESEARCH_PASS_NOT_LIVE" if passing else "REJECT"

    def serial_candidate(c: Candidate) -> dict:
        return {**asdict(c), "id": c.id}

    report = {
        "engine": "LIQUIDITY_FACTORY_V2",
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "checksumVerification": True,
        "source": "Binance Public Data / data.binance.vision Spot monthly 15m klines",
        "window": {
            "start": args.start,
            "end": args.end,
            "timeframe": "15m",
            "split": {"discovery": 0.60, "validation": 0.20, "oos": 0.20},
        },
        "selectionProtocol": {
            "oosUsedForSelection": False,
            "frozenBeforeOos": True,
            "baseCosts": asdict(base_cost),
            "stressCosts": asdict(stress_cost),
            "risk": asdict(risk),
        },
        "componentLibrary": {
            "candidateCount": len(library),
            "htfRegimes": HTF_REGIMES,
            "discounts": DISCOUNTS,
            "sweepLookbacks": SWEEP_LOOKBACKS,
            "sweepDepthAtr": SWEEP_DEPTHS,
            "chochLookbacks": CHOCH_LOOKBACKS,
            "participation": PARTICIPATION,
        },
        "data": data_meta,
        "gates": {
            "discovery": "trades>=80, PF>=1.15, avg>0, median>-0.05%, DD<=5%, >=3 positive symbols",
            "validation": "trades>=25, PF>=1.10, avg>0, DD<=4%, >=3 positive symbols",
            "oos": "trades>=25, PF>=1.15, avg>0, DD<=3%, >=3 positive symbols; stress PF>=1.0 and avg>0",
        },
        "discovery": {
            "tested": len(library),
            "eligible": len(eligible),
            "top": [
                {"candidate": serial_candidate(x["candidate"]), "metrics": x["result"]["aggregate"]}
                for x in eligible[: args.top_discovery]
            ],
        },
        "validation": {
            "tested": len(validation_rows),
            "passed": len(passed_validation),
            "frozenFinalists": [serial_candidate(x["candidate"]) for x in frozen],
            "rows": [
                {
                    "candidate": serial_candidate(x["candidate"]),
                    "pass": x["pass"],
                    "freezeScore": x["freezeScore"],
                    "discovery": x["discovery"]["aggregate"],
                    "validation": x["validation"]["aggregate"],
                }
                for x in validation_rows
            ],
        },
        "oos": {
            "tested": len(oos_rows),
            "passed": len(passing),
            "rows": [
                {
                    "candidate": serial_candidate(x["candidate"]),
                    "pass": x["pass"],
                    "base": x["base"]["aggregate"],
                    "stress": x["stress"]["aggregate"],
                    "basePerSymbol": x["base"]["perSymbol"],
                }
                for x in oos_rows
            ],
        },
        "promotionGate": {
            "decision": decision,
            "passingCandidates": [serial_candidate(x["candidate"]) for x in passing],
            "canEnableLiveTrading": False,
            "nextStage": "PAPER_REVIEW_ONLY" if passing else "NONE",
        },
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "candidateCount": len(library),
        "discoveryEligible": len(eligible),
        "validationPassed": len(passed_validation),
        "frozenFinalists": [x["candidate"].id for x in frozen],
        "oosPassed": len(passing),
        "promotionGate": report["promotionGate"],
    }, indent=2))


if __name__ == "__main__":
    main()
