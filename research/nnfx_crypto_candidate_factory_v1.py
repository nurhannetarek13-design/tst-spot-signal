#!/usr/bin/env python3
"""NNFX-inspired crypto candidate factory for Binance Spot research.

This is deliberately NOT an NNFX clone and NOT a live strategy. It borrows the
modular research idea: baseline + confirmation + participation filter +
volatility filter + exit, then freezes candidates before OOS.

Safety / research contract:
- Binance Spot long-only semantics, no leverage.
- Public Binance Vision monthly 15m archives only; no API keys/order endpoints.
- Signals form on a completed bar; discretionary entry/exit signals execute on
  the next bar open. Protective ATR stop/target may trigger intrabar.
- 0.10% commission per side + 0.05% slippage per side in base tests.
- Discovery 60% -> Validation 20% -> frozen Finalists -> OOS 20%.
- OOS is never used to select/freeze finalists.
- Frozen finalists receive an additional OOS cost stress test.
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
UA = "tst-nnfx-crypto-candidate-factory-v1/1.0"
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
    stop_atr: float = 1.50
    target_atr: float = 2.50
    max_bars: int = 96


@dataclass(frozen=True)
class Candidate:
    baseline: str
    confirmation: str
    participation: str
    volatility: str
    exit_mode: str

    @property
    def id(self) -> str:
        return "__".join([
            self.baseline,
            self.confirmation,
            self.participation,
            self.volatility,
            self.exit_mode,
        ])


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
        raise RuntimeError(f"unexpected kline columns: {df.shape[1]}")
    df = df.iloc[:, : len(KLINE_COLS)].copy()
    df.columns = KLINE_COLS
    numeric_cols = [
        "open", "high", "low", "close", "volume", "quote_volume",
        "taker_buy_base", "taker_buy_quote",
    ]
    for c in numeric_cols:
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
            sources.append({
                "month": stamp, "url": url, "status": "OK", "rows": len(frame),
                "checksumVerified": True,
            })
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                sources.append({
                    "month": stamp, "url": url, "status": "MISSING_404", "rows": 0,
                    "checksumVerified": False,
                })
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


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_down = down.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema34"] = d["close"].ewm(span=34, adjust=False, min_periods=34).mean()
    d["ema55"] = d["close"].ewm(span=55, adjust=False, min_periods=55).mean()
    hh20 = d["high"].rolling(20, min_periods=20).max()
    ll20 = d["low"].rolling(20, min_periods=20).min()
    d["donchian_mid20"] = (hh20 + ll20) / 2.0

    d["rsi14"] = wilder_rsi(d["close"], 14)
    ema12 = d["close"].ewm(span=12, adjust=False, min_periods=26).mean()
    ema26 = d["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    d["macd_hist"] = macd - signal
    d["roc12"] = d["close"].pct_change(12)

    qv_mean20 = d["quote_volume"].rolling(20, min_periods=20).mean()
    d["rel_volume20"] = d["quote_volume"] / qv_mean20.replace(0, np.nan)
    d["taker_share"] = d["taker_buy_quote"] / d["quote_volume"].replace(0, np.nan)
    direction = np.sign(d["close"].diff()).fillna(0.0)
    d["obv"] = (direction * d["volume"]).cumsum()
    d["obv_up10"] = d["obv"] > d["obv"].shift(10)

    tr = true_range(d)
    d["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["atr_pct"] = d["atr14"] / d["close"]
    d["atr_pct_med50"] = d["atr_pct"].rolling(50, min_periods=50).median()

    mid = d["close"].rolling(20, min_periods=20).mean()
    sd = d["close"].rolling(20, min_periods=20).std(ddof=0)
    upper = mid + 2 * sd
    lower = mid - 2 * sd
    d["bb_width"] = (upper - lower) / mid.replace(0, np.nan)
    d["bb_width_med50"] = d["bb_width"].rolling(50, min_periods=50).median()

    return d


BASELINES = ["ema34", "ema55", "donchian20"]
CONFIRMATIONS = ["rsi55", "macd_pos", "roc_pos"]
PARTICIPATION = ["taker53", "relvol110", "obv_up"]
VOLATILITY = ["atr_normal", "bb_expand", "atr_rising"]
EXITS = ["baseline", "rsi48", "macd_neg"]


def candidates() -> list[Candidate]:
    return [Candidate(*x) for x in itertools.product(
        BASELINES, CONFIRMATIONS, PARTICIPATION, VOLATILITY, EXITS
    )]


def baseline_series(d: pd.DataFrame, name: str) -> pd.Series:
    if name == "ema34":
        return d["ema34"]
    if name == "ema55":
        return d["ema55"]
    if name == "donchian20":
        return d["donchian_mid20"]
    raise KeyError(name)


def candidate_masks(d: pd.DataFrame, c: Candidate):
    base = baseline_series(d, c.baseline)
    baseline_ok = (d["close"] > base) & (base > base.shift(4))
    baseline_cross = (d["close"].shift(1) <= base.shift(1)) & (d["close"] > base)

    conf = {
        "rsi55": (d["rsi14"] >= 55) & (d["rsi14"] > d["rsi14"].shift(1)),
        "macd_pos": (d["macd_hist"] > 0) & (d["macd_hist"] > d["macd_hist"].shift(1)),
        "roc_pos": d["roc12"] > 0,
    }[c.confirmation]

    participation = {
        "taker53": d["taker_share"] >= 0.53,
        "relvol110": d["rel_volume20"] >= 1.10,
        "obv_up": d["obv_up10"].fillna(False),
    }[c.participation]

    vol = {
        "atr_normal": (d["atr_pct"] >= 0.0015) & (d["atr_pct"] <= 0.0300),
        "bb_expand": (d["bb_width"] > d["bb_width_med50"]) & (d["atr_pct"] <= 0.0300),
        "atr_rising": (d["atr_pct"] > d["atr_pct_med50"]) & (d["atr_pct"] <= 0.0300),
    }[c.volatility]

    entry = baseline_ok & baseline_cross & conf & participation & vol
    exit_signal = {
        "baseline": d["close"] < base,
        "rsi48": d["rsi14"] < 48,
        "macd_neg": d["macd_hist"] < 0,
    }[c.exit_mode]
    return entry.fillna(False).to_numpy(bool), exit_signal.fillna(False).to_numpy(bool)


def simulate(symbol: str, d: pd.DataFrame, c: Candidate, cost: CostModel, risk: RiskModel) -> list[Trade]:
    entry_mask, exit_mask = candidate_masks(d, c)
    trades: list[Trade] = []
    position = None
    pending_entry = False
    pending_exit = False
    warmup = 90

    for i in range(warmup, len(d)):
        row = d.iloc[i]

        # Execute close-generated orders at the next bar open.
        if pending_exit and position is not None:
            exit_px = float(row["open"]) * (1.0 - cost.slippage_rate)
            qty = position["qty"]
            gross = qty * (exit_px - position["entry"])
            fees = cost.fee_rate * qty * (position["entry"] + exit_px)
            pnl = gross - fees
            trades.append(Trade(
                symbol=symbol,
                entry_ts=str(position["entry_ts"]),
                exit_ts=str(row["ts"]),
                entry=position["entry"],
                exit=exit_px,
                qty=qty,
                pnl_usdt=pnl,
                return_pct=pnl / (qty * position["entry"]) * 100.0,
                bars_held=i - position["entry_i"],
                reason="signal_exit",
            ))
            position = None
            pending_exit = False

        if pending_entry and position is None:
            entry_px = float(row["open"]) * (1.0 + cost.slippage_rate)
            atr = float(d.iloc[i - 1]["atr14"])
            if math.isfinite(atr) and atr > 0 and entry_px > 0:
                qty = risk.trade_usdt / entry_px
                position = {
                    "entry": entry_px,
                    "qty": qty,
                    "entry_i": i,
                    "entry_ts": row["ts"],
                    "entry_atr": atr,
                }
            pending_entry = False

        if position is not None:
            entry = position["entry"]
            atr = position["entry_atr"]
            stop = entry - risk.stop_atr * atr
            target = entry + risk.target_atr * atr
            low = float(row["low"])
            high = float(row["high"])

            # Conservative ambiguity handling: stop wins if stop and target are both touched.
            if low <= stop:
                exit_px = stop * (1.0 - cost.slippage_rate)
                qty = position["qty"]
                gross = qty * (exit_px - entry)
                fees = cost.fee_rate * qty * (entry + exit_px)
                pnl = gross - fees
                trades.append(Trade(
                    symbol, str(position["entry_ts"]), str(row["ts"]), entry, exit_px, qty, pnl,
                    pnl / (qty * entry) * 100.0, i - position["entry_i"], "atr_stop"
                ))
                position = None
                pending_exit = False
                continue
            if high >= target:
                exit_px = target
                qty = position["qty"]
                gross = qty * (exit_px - entry)
                fees = cost.fee_rate * qty * (entry + exit_px)
                pnl = gross - fees
                trades.append(Trade(
                    symbol, str(position["entry_ts"]), str(row["ts"]), entry, exit_px, qty, pnl,
                    pnl / (qty * entry) * 100.0, i - position["entry_i"], "atr_target"
                ))
                position = None
                pending_exit = False
                continue
            if i - position["entry_i"] >= risk.max_bars:
                pending_exit = True
            elif exit_mask[i]:
                pending_exit = True
            continue

        if i + 1 < len(d) and entry_mask[i]:
            pending_entry = True

    if position is not None:
        row = d.iloc[-1]
        exit_px = float(row["close"]) * (1.0 - cost.slippage_rate)
        qty = position["qty"]
        gross = qty * (exit_px - position["entry"])
        fees = cost.fee_rate * qty * (position["entry"] + exit_px)
        pnl = gross - fees
        trades.append(Trade(
            symbol, str(position["entry_ts"]), str(row["ts"]), position["entry"], exit_px, qty, pnl,
            pnl / (qty * position["entry"]) * 100.0, len(d) - 1 - position["entry_i"], "segment_end"
        ))
    return trades


def metrics(trades: list[Trade], initial_cash: float = 100.0) -> dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "winRatePct": 0.0,
            "netPnlUsdt": 0.0, "avgTradePct": 0.0, "medianTradePct": 0.0,
            "profitFactor": 0.0, "maxDrawdownPct": 0.0,
        }
    ordered = sorted(trades, key=lambda t: t.exit_ts)
    pnls = np.array([x.pnl_usdt for x in ordered], dtype=float)
    rets = np.array([x.return_pct for x in ordered], dtype=float)
    gp = float(pnls[pnls > 0].sum()) if np.any(pnls > 0) else 0.0
    gl = float(abs(pnls[pnls < 0].sum())) if np.any(pnls < 0) else 0.0
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    equity = initial_cash + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[initial_cash], equity]))[1:]
    dd_pct = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)
    return {
        "trades": int(len(ordered)),
        "wins": int((pnls > 0).sum()),
        "losses": int((pnls < 0).sum()),
        "winRatePct": float((pnls > 0).mean() * 100.0),
        "netPnlUsdt": float(pnls.sum()),
        "avgTradePct": float(rets.mean()),
        "medianTradePct": float(np.median(rets)),
        "profitFactor": float(pf),
        "maxDrawdownPct": float(dd_pct.max()) if len(dd_pct) else 0.0,
    }


def evaluate_candidate(frames: dict[str, pd.DataFrame], c: Candidate, cost: CostModel, risk: RiskModel):
    all_trades: list[Trade] = []
    by_symbol = {}
    for symbol, frame in frames.items():
        ts = simulate(symbol, frame, c, cost, risk)
        all_trades.extend(ts)
        by_symbol[symbol] = metrics(ts)
    agg = metrics(all_trades)
    agg["positiveSymbols"] = sum(1 for m in by_symbol.values() if m["netPnlUsdt"] > 0 and m["profitFactor"] >= 1.0)
    return {"aggregate": agg, "bySymbol": by_symbol}


def score_result(r: dict) -> float:
    m = r["aggregate"]
    if m["trades"] <= 0:
        return -1e9
    pf_term = math.log(max(1e-6, min(m["profitFactor"], 10.0)))
    exp_term = m["avgTradePct"] * 2.0
    breadth = m.get("positiveSymbols", 0) * 0.25
    dd_penalty = m["maxDrawdownPct"] * 0.08
    sample = math.log1p(m["trades"]) * 0.15
    return pf_term + exp_term + breadth + sample - dd_penalty


def discovery_eligible(r: dict) -> bool:
    m = r["aggregate"]
    return (
        m["trades"] >= 100
        and m["profitFactor"] >= 1.05
        and m["avgTradePct"] > 0
        and m["positiveSymbols"] >= 3
        and m["maxDrawdownPct"] <= 12.0
    )


def validation_pass(r: dict) -> bool:
    m = r["aggregate"]
    return (
        m["trades"] >= 40
        and m["profitFactor"] >= 1.10
        and m["avgTradePct"] > 0
        and m["positiveSymbols"] >= 3
        and m["maxDrawdownPct"] <= 10.0
    )


def oos_pass(base: dict, stress: dict) -> bool:
    m = base["aggregate"]
    s = stress["aggregate"]
    return (
        m["trades"] >= 40
        and m["profitFactor"] >= 1.20
        and m["avgTradePct"] > 0
        and m["positiveSymbols"] >= 3
        and m["maxDrawdownPct"] <= 10.0
        and s["profitFactor"] >= 1.00
        and s["avgTradePct"] >= 0
        and s["positiveSymbols"] >= 2
    )


def split_frames(frames: dict[str, pd.DataFrame]):
    out = {"DISCOVERY": {}, "VALIDATION": {}, "OOS": {}}
    meta = {}
    for symbol, d in frames.items():
        n = len(d)
        a = int(n * 0.60)
        b = int(n * 0.80)
        out["DISCOVERY"][symbol] = d.iloc[:a].reset_index(drop=True)
        out["VALIDATION"][symbol] = d.iloc[a:b].reset_index(drop=True)
        out["OOS"][symbol] = d.iloc[b:].reset_index(drop=True)
        meta[symbol] = {
            "rows": n,
            "discoveryRows": a,
            "validationRows": b - a,
            "oosRows": n - b,
            "discoveryEnd": str(d.iloc[a - 1]["ts"]),
            "validationStart": str(d.iloc[a]["ts"]),
            "validationEnd": str(d.iloc[b - 1]["ts"]),
            "oosStart": str(d.iloc[b]["ts"]),
        }
    return out, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"])
    ap.add_argument("--start", default="2024-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--top-discovery", type=int, default=20)
    ap.add_argument("--top-validation", type=int, default=8)
    ap.add_argument("--output", default="artifacts/nnfx-crypto-factory-v1/report.json")
    args = ap.parse_args()

    base_cost = CostModel()
    stress_cost = CostModel(fee_rate=0.0015, slippage_rate=0.0010)
    risk = RiskModel()
    frames = {}
    data_meta = {}

    for symbol in args.symbols:
        print(f"Loading {symbol} from Binance Vision...")
        raw, sources, coverage = fetch_spot_klines(symbol, args.start, args.end)
        feat = add_features(raw)
        frames[symbol] = feat
        data_meta[symbol] = {
            "coverage": coverage,
            "rows": len(feat),
            "start": str(feat.iloc[0]["ts"]),
            "end": str(feat.iloc[-1]["ts"]),
            "sources": sources,
        }

    split, split_meta = split_frames(frames)
    universe = candidates()
    print(f"Candidate count: {len(universe)}")

    discovery_rows = []
    for idx, c in enumerate(universe, 1):
        r = evaluate_candidate(split["DISCOVERY"], c, base_cost, risk)
        row = {"candidate": asdict(c), "candidateId": c.id, "result": r, "score": score_result(r)}
        discovery_rows.append(row)
        if idx % 20 == 0:
            print(f"Discovery {idx}/{len(universe)}")

    discovery_eligible_rows = [x for x in discovery_rows if discovery_eligible(x["result"])]
    discovery_eligible_rows.sort(key=lambda x: x["score"], reverse=True)
    discovery_shortlist = discovery_eligible_rows[: args.top_discovery]

    validation_rows = []
    for x in discovery_shortlist:
        c = Candidate(**x["candidate"])
        r = evaluate_candidate(split["VALIDATION"], c, base_cost, risk)
        validation_rows.append({
            "candidate": x["candidate"], "candidateId": c.id,
            "discovery": x["result"], "validation": r,
            "validationScore": score_result(r),
            "passed": validation_pass(r),
        })

    validation_passed = [x for x in validation_rows if x["passed"]]
    validation_passed.sort(key=lambda x: x["validationScore"], reverse=True)
    frozen = validation_passed[: args.top_validation]

    # OOS is touched only after the finalist set has been frozen above.
    oos_rows = []
    for x in frozen:
        c = Candidate(**x["candidate"])
        base = evaluate_candidate(split["OOS"], c, base_cost, risk)
        stress = evaluate_candidate(split["OOS"], c, stress_cost, risk)
        passed = oos_pass(base, stress)
        oos_rows.append({
            "candidate": x["candidate"], "candidateId": c.id,
            "discovery": x["discovery"], "validation": x["validation"],
            "oos": base, "oosCostStress": stress, "passed": passed,
        })

    promoted = [x for x in oos_rows if x["passed"]]
    promoted.sort(key=lambda x: score_result(x["oos"]), reverse=True)

    report = {
        "authorization": AUTHORIZATION,
        "liveTrading": False,
        "engine": "NNFX_CRYPTO_CANDIDATE_FACTORY_V1",
        "inspiration": "modular baseline/confirmation/filter/exit research architecture; not an NNFX clone",
        "source": "Binance Public Data / data.binance.vision Spot monthly 15m klines",
        "checksumVerification": True,
        "window": {"start": args.start, "end": args.end, "timeframe": "15m"},
        "costModel": asdict(base_cost),
        "stressCostModel": asdict(stress_cost),
        "riskModel": asdict(risk),
        "componentLibrary": {
            "baselines": BASELINES,
            "confirmations": CONFIRMATIONS,
            "participation": PARTICIPATION,
            "volatility": VOLATILITY,
            "exits": EXITS,
            "candidateCount": len(universe),
        },
        "selectionProtocol": {
            "discoveryFraction": 0.60,
            "validationFraction": 0.20,
            "oosFraction": 0.20,
            "topDiscovery": args.top_discovery,
            "topValidationFrozenBeforeOOS": args.top_validation,
            "oosUsedForSelection": False,
            "discoveryGate": {
                "tradesMin": 100, "profitFactorMin": 1.05, "avgTradePct": ">0",
                "positiveSymbolsMin": 3, "maxDrawdownPctMax": 12.0,
            },
            "validationGate": {
                "tradesMin": 40, "profitFactorMin": 1.10, "avgTradePct": ">0",
                "positiveSymbolsMin": 3, "maxDrawdownPctMax": 10.0,
            },
            "oosGate": {
                "tradesMin": 40, "profitFactorMin": 1.20, "avgTradePct": ">0",
                "positiveSymbolsMin": 3, "maxDrawdownPctMax": 10.0,
                "stressProfitFactorMin": 1.00, "stressAvgTradePct": ">=0",
                "stressPositiveSymbolsMin": 2,
            },
        },
        "data": data_meta,
        "splitMeta": split_meta,
        "discovery": {
            "tested": len(discovery_rows),
            "eligible": len(discovery_eligible_rows),
            "shortlisted": len(discovery_shortlist),
            "top": discovery_shortlist,
        },
        "validation": {
            "tested": len(validation_rows),
            "passed": len(validation_passed),
            "frozenFinalists": [x["candidateId"] for x in frozen],
            "results": validation_rows,
        },
        "oos": {
            "testedFrozenFinalists": len(oos_rows),
            "passed": len(promoted),
            "results": oos_rows,
        },
        "promotionGate": {
            "decision": "RESEARCH_PASS_NOT_LIVE" if promoted else "REJECT",
            "passingCandidates": [x["candidateId"] for x in promoted],
            "canEnableLiveTrading": False,
            "nextStage": "FORWARD_SHADOW_INCUBATION" if promoted else "NONE",
        },
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")

    summary = {
        "candidateCount": len(universe),
        "discoveryEligible": len(discovery_eligible_rows),
        "validationPassed": len(validation_passed),
        "frozenFinalists": [x["candidateId"] for x in frozen],
        "oosPassed": len(promoted),
        "promotionGate": report["promotionGate"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
