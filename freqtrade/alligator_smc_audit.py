#!/usr/bin/env python3
import argparse, json, math, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

BASE_URLS = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]
DEFAULT_SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","TRXUSDT",
]
FEE_SIDE = 0.001
EXECUTION_SIDE = 0.0005  # conservative spread+slippage buffer per side
ATR_STOP_MULT = 1.5
MAX_HOLD_BARS = 96


def http_json(path, params):
    q = urlencode(params)
    last = None
    for base in BASE_URLS:
        try:
            req = Request(
                f"{base}{path}?{q}",
                headers={"User-Agent": "tst-alligator-audit/1.0", "Accept": "application/json"},
            )
            with urlopen(req, timeout=30) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP_{r.status}")
                return json.loads(r.read().decode())
        except Exception as exc:
            last = exc
    raise RuntimeError(f"BINANCE_UNAVAILABLE:{type(last).__name__}:{last}")


def download_klines(symbol, start_ms, end_ms, interval="15m"):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        batch = http_json(
            "/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][6]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.02)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame({
        "open_time": [int(x[0]) for x in rows],
        "open": [float(x[1]) for x in rows],
        "high": [float(x[2]) for x in rows],
        "low": [float(x[3]) for x in rows],
        "close": [float(x[4]) for x in rows],
        "volume": [float(x[5]) for x in rows],
        "close_time": [int(x[6]) for x in rows],
        "quote_volume": [float(x[7]) for x in rows],
        "taker_buy_quote": [float(x[10]) for x in rows],
    }).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def smma(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def atr(df, n=14):
    prev = df.close.shift(1)
    tr = pd.concat([
        df.high - df.low,
        (df.high - prev).abs(),
        (df.low - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def psar(df, step=0.02, maximum=0.2):
    h = df.high.to_numpy(float)
    l = df.low.to_numpy(float)
    n = len(df)
    out = np.full(n, np.nan)
    if n == 0:
        return pd.Series(out, index=df.index)
    if n == 1:
        out[0] = l[0]
        return pd.Series(out, index=df.index)
    bull, af, ep = True, step, h[0]
    out[0] = l[0]
    for i in range(1, n):
        p = out[i - 1] + af * (ep - out[i - 1])
        if bull:
            p = min(p, l[i - 1])
            if i > 1:
                p = min(p, l[i - 2])
            if l[i] < p:
                bull, p, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(maximum, af + step)
        else:
            p = max(p, h[i - 1])
            if i > 1:
                p = max(p, h[i - 2])
            if h[i] > p:
                bull, p, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(maximum, af + step)
        out[i] = p
    return pd.Series(out, index=df.index)


def add_htf_bias(df):
    tmp = df.copy()
    tmp["dt"] = pd.to_datetime(tmp.open_time, unit="ms", utc=True)
    tmp["bucket"] = tmp.dt.dt.floor("4h")
    g = tmp.groupby("bucket", sort=True).agg(close=("close", "last"))
    g["ema20"] = g.close.ewm(span=20, adjust=False, min_periods=20).mean()
    g["ema20_prev"] = g.ema20.shift(1)
    g["htf_bias"] = (g.close > g.ema20) & (g.ema20 > g.ema20_prev)
    g["available_ms"] = (g.index + pd.Timedelta(hours=4)).astype("int64") // 10**6
    bias = pd.merge_asof(
        df[["close_time"]].sort_values("close_time"),
        g[["available_ms", "htf_bias"]].dropna().sort_values("available_ms"),
        left_on="close_time",
        right_on="available_ms",
        direction="backward",
    )["htf_bias"].fillna(False).astype(bool)
    return bias.reset_index(drop=True)


def add_structure_columns(df):
    n = len(df)
    sweep = np.zeros(n, dtype=bool)
    bos = np.zeros(n, dtype=bool)
    location = np.zeros(n, dtype=bool)
    room = np.zeros(n, dtype=bool)
    valid_zone = np.zeros(n, dtype=bool)
    stop = np.full(n, np.nan)
    risk_pct = np.full(n, np.nan)
    demand_low = demand_high = None
    seed = last_sweep = last_shift = -10000
    last_sweep_low = None
    H, L, O, C = (df.high.to_numpy(float), df.low.to_numpy(float), df.open.to_numpy(float), df.close.to_numpy(float))
    A, R = df.atr.to_numpy(float), df.relvol.to_numpy(float)
    for j in range(n):
        a = A[j]
        if demand_low is not None and ((j - seed) > 96 or C[j] < demand_low):
            demand_low = demand_high = None
            seed = last_sweep = last_shift = -10000
            last_sweep_low = None
        if j < 12 or not np.isfinite(a) or a <= 0:
            continue
        prior_low = float(np.min(L[j - 12:j]))
        prior_high = float(np.max(H[j - 12:j]))
        if demand_low is not None:
            touched = L[j] <= demand_high * 1.002 and C[j] > demand_low
            if L[j] < prior_low and C[j] > prior_low and touched:
                last_sweep, last_sweep_low = j, float(L[j])
            if 0 < j - last_sweep <= 8 and C[j] > prior_high:
                last_shift = j
        body = C[j] - O[j]
        displaced = (
            demand_low is None
            and C[j] > prior_high
            and body >= 0.80 * a
            and np.isfinite(R[j])
            and R[j] >= 1.10
            and j >= 1
            and C[j - 1] < O[j - 1]
        )
        if displaced:
            demand_low, demand_high, seed = float(L[j - 1]), float(O[j - 1]), j
            last_sweep = last_shift = -10000
            last_sweep_low = None
        if demand_low is None:
            continue
        valid_zone[j] = True
        sweep[j] = 0 <= j - last_sweep <= 8
        bos[j] = 0 <= j - last_shift <= 6
        dist = C[j] - demand_high
        location[j] = C[j] >= demand_high and 0 <= dist <= 3.0 * a
        inv = min(demand_low, last_sweep_low if last_sweep_low is not None else demand_low)
        s = max(0.0, inv - 0.20 * a)
        stop[j] = s
        risk = C[j] - s
        rp = risk / C[j] if C[j] > 0 and risk > 0 else np.nan
        risk_pct[j] = rp
        p48 = float(np.max(H[max(0, j - 48):j])) if j > 0 else C[j]
        overhead = p48 - C[j]
        room[j] = bool(risk > 0 and np.isfinite(rp) and rp <= 0.03 and (p48 <= C[j] or overhead >= 2.0 * risk))
    df["zone_valid"] = valid_zone
    df["sweep_ok"] = sweep
    df["bos_ok"] = bos
    df["location_ok"] = location
    df["room_2r"] = room
    df["structure_stop"] = stop
    df["structure_risk_pct"] = risk_pct


def btc_regime_frame(btc_df):
    c = btc_df.close
    ema = c.ewm(span=200, adjust=False, min_periods=200).mean()
    return pd.DataFrame({"close_time": btc_df.close_time, "btc_regime": (c > ema).fillna(False)})


def add_features(df, btc_regime_by_close=None):
    df = df.copy()
    c = df.close
    df["jaw"], df["teeth"], df["lips"] = smma(c, 13), smma(c, 8), smma(c, 5)
    df["spread"] = (df.lips - df.jaw) / c
    ema12 = c.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = c.ewm(span=26, adjust=False, min_periods=26).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df.macd.ewm(span=9, adjust=False, min_periods=9).mean()
    df["ema200"] = c.ewm(span=200, adjust=False, min_periods=200).mean()
    df["atr"] = atr(df)
    df["psar"] = psar(df)
    df["relvol"] = df.volume / df.volume.rolling(20).mean().shift(1)
    df["taker_share"] = (df.taker_buy_quote / df.quote_volume.replace(0, np.nan)).fillna(0)
    df["alligator"] = (
        (df.lips > df.teeth) & (df.teeth > df.jaw)
        & (df.lips > df.lips.shift(1)) & (df.teeth > df.teeth.shift(1))
        & (df.jaw > df.jaw.shift(1)) & (df.spread > df.spread.shift(1))
    )
    df["macd_ok"] = df.macd > df.macd_signal
    df["sar_ok"] = df.psar < df.close
    df["vol_ok"] = df.relvol >= 1.2
    df["ema_ok"] = df.close > df.ema200
    df["htf_ok"] = add_htf_bias(df)
    htf_proxy = c.ewm(span=320, adjust=False, min_periods=320).mean()
    df["trend_exit"] = (df.lips < df.teeth) | (df.macd < df.macd_signal) | (df.psar > df.close) | (df.close < htf_proxy)
    add_structure_columns(df)
    if btc_regime_by_close is not None:
        aligned = pd.merge_asof(
            df[["close_time"]].sort_values("close_time"),
            btc_regime_by_close[["close_time", "btc_regime"]].sort_values("close_time"),
            on="close_time",
            direction="backward",
        )
        df["btc_ok"] = aligned.btc_regime.fillna(False).to_numpy(bool)
    else:
        df["btc_ok"] = True
    return df


def gate_masks(df):
    m = {}
    m["A_ALLIGATOR"] = df.alligator
    m["B_MACD"] = m["A_ALLIGATOR"] & df.macd_ok
    m["C_SAR"] = m["B_MACD"] & df.sar_ok
    m["D_VOLUME"] = m["C_SAR"] & df.vol_ok
    m["E_EMA_HTF"] = m["D_VOLUME"] & df.ema_ok & df.htf_ok
    m["F_SMC_LOCATION_ROOM"] = m["E_EMA_HTF"] & df.zone_valid & df.location_ok & df.room_2r
    m["G_LIQUIDITY_SWEEP"] = m["F_SMC_LOCATION_ROOM"] & df.sweep_ok
    m["H_BOS"] = m["G_LIQUIDITY_SWEEP"] & df.bos_ok
    m["I_BTC_REGIME"] = m["H_BOS"] & df.btc_ok
    m["J_L2"] = None
    m["K_TAKER_FLOW"] = m["I_BTC_REGIME"] & (df.taker_share >= 0.56)
    return m


@dataclass
class Trade:
    symbol: str
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    pnl_pct: float
    gross_pct: float
    r_multiple: float
    reason: str
    mfe_pct: float
    mae_pct: float
    hold_bars: int


def backtest_symbol(df, symbol, mask, structural=False):
    trades, i, n = [], 360, len(df)
    while i < n - 1:
        if not bool(mask.iloc[i]):
            i += 1
            continue
        ent_i = i + 1
        raw_entry = float(df.open.iloc[ent_i])
        entry = raw_entry * (1 + EXECUTION_SIDE)
        a = float(df.atr.iloc[i])
        if not np.isfinite(a) or a <= 0:
            i += 1
            continue
        if structural:
            raw_stop = float(df.structure_stop.iloc[i])
            if not np.isfinite(raw_stop) or raw_stop <= 0 or raw_stop >= raw_entry:
                i += 1
                continue
        else:
            raw_stop = raw_entry - ATR_STOP_MULT * a
        risk = entry - raw_stop
        if risk <= 0:
            i += 1
            continue
        raw_target = entry + 2.0 * risk
        max_h = min_l = raw_entry
        exit_px = reason = exit_i = None
        for j in range(ent_i, min(n, ent_i + MAX_HOLD_BARS)):
            lo, hi = float(df.low.iloc[j]), float(df.high.iloc[j])
            max_h, min_l = max(max_h, hi), min(min_l, lo)
            if lo <= raw_stop and hi >= raw_target:
                exit_px, reason, exit_i = raw_stop * (1 - EXECUTION_SIDE), "AMBIG_STOP_FIRST", j
                break
            if lo <= raw_stop:
                exit_px, reason, exit_i = raw_stop * (1 - EXECUTION_SIDE), "STOP", j
                break
            if hi >= raw_target:
                exit_px, reason, exit_i = raw_target * (1 - EXECUTION_SIDE), "TARGET_2R", j
                break
            if structural and j > ent_i and bool(df.trend_exit.iloc[j]):
                exit_px, reason, exit_i = float(df.close.iloc[j]) * (1 - EXECUTION_SIDE), "TREND_FAILURE", j
                break
        if exit_px is None:
            exit_i = min(n - 1, ent_i + MAX_HOLD_BARS - 1)
            exit_px, reason = float(df.close.iloc[exit_i]) * (1 - EXECUTION_SIDE), "TIME"
        gross = (exit_px - entry) / entry
        net = gross - 2 * FEE_SIDE
        mfe, mae = (max_h - entry) / entry, (min_l - entry) / entry
        risk_frac = (entry - raw_stop) / entry
        r_mult = net / risk_frac if risk_frac > 0 else np.nan
        trades.append(Trade(
            symbol, int(df.open_time.iloc[ent_i]), int(df.close_time.iloc[exit_i]),
            entry, exit_px, net, gross, r_mult, reason, mfe, mae, exit_i - ent_i + 1,
        ))
        i = exit_i + 1
    return trades


def metrics(trades):
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": None,
            "avg_win_pct": None, "avg_loss_pct": None, "payoff": None,
            "expectancy_pct": None, "expectancy_r": None, "profit_factor": None,
            "net_return_sum_pct": 0.0, "max_drawdown_pct": None,
            "longest_losing_streak": 0, "sharpe_trade": None, "sortino_trade": None,
            "median_trade_pct": None, "median_mfe_pct": None, "median_mae_pct": None,
            "avg_hold_bars": None, "target_hit_rate": None, "break_even_win_rate": None,
        }
    x = np.array([t.pnl_pct for t in trades], float)
    r = np.array([t.r_multiple for t in trades], float)
    wins, losses = x[x > 0], x[x <= 0]
    aw = float(wins.mean()) if len(wins) else 0.0
    al = float(losses.mean()) if len(losses) else 0.0
    payoff = aw / abs(al) if len(wins) and len(losses) and al != 0 else None
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else None
    eq = np.cumprod(1 + x)
    peaks = np.maximum.accumulate(eq)
    mdd = float((eq / peaks - 1).min()) if len(eq) else 0.0
    streak = mx = 0
    for v in x:
        if v <= 0:
            streak += 1
            mx = max(mx, streak)
        else:
            streak = 0
    sd = float(x.std(ddof=1)) if len(x) > 1 else 0.0
    sharpe = float(x.mean() / sd * math.sqrt(len(x))) if sd > 0 else None
    neg = x[x < 0]
    dsd = float(neg.std(ddof=1)) if len(neg) > 1 else 0.0
    sortino = float(x.mean() / dsd * math.sqrt(len(x))) if dsd > 0 else None
    be = 1 / (1 + payoff) if payoff and payoff > 0 else None
    return {
        "trades": len(trades), "wins": int((x > 0).sum()), "losses": int((x <= 0).sum()),
        "win_rate": float((x > 0).mean()), "avg_win_pct": aw, "avg_loss_pct": al,
        "payoff": payoff, "expectancy_pct": float(x.mean()), "expectancy_r": float(np.nanmean(r)),
        "profit_factor": pf, "net_return_sum_pct": float(x.sum()), "max_drawdown_pct": mdd,
        "longest_losing_streak": mx, "sharpe_trade": sharpe, "sortino_trade": sortino,
        "median_trade_pct": float(np.median(x)),
        "median_mfe_pct": float(np.median([t.mfe_pct for t in trades])),
        "median_mae_pct": float(np.median([t.mae_pct for t in trades])),
        "avg_hold_bars": float(np.mean([t.hold_bars for t in trades])),
        "target_hit_rate": float(np.mean([t.reason == "TARGET_2R" for t in trades])),
        "break_even_win_rate": be,
    }


def run(args):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    print(f"AUDIT_WINDOW {start.isoformat()} -> {end.isoformat()} days={args.days}", flush=True)
    raw = {}
    for s in args.symbols:
        print(f"DOWNLOAD {s}", flush=True)
        raw[s] = download_klines(s, start_ms, end_ms)
        print(f"ROWS {s} {len(raw[s])}", flush=True)
    if "BTCUSDT" not in raw or raw["BTCUSDT"].empty:
        raise RuntimeError("BTCUSDT required")
    btc_reg = btc_regime_frame(raw["BTCUSDT"])
    feats = {}
    for s, df in raw.items():
        print(f"FEATURES {s}", flush=True)
        feats[s] = add_features(df, btc_reg)

    stages = [
        "A_ALLIGATOR", "B_MACD", "C_SAR", "D_VOLUME", "E_EMA_HTF",
        "F_SMC_LOCATION_ROOM", "G_LIQUIDITY_SWEEP", "H_BOS", "I_BTC_REGIME", "K_TAKER_FLOW",
    ]
    total_bars = sum(max(0, len(df) - 360) for df in feats.values())
    funnel = {}
    for stage in stages:
        passed = sum(int(gate_masks(df)[stage].iloc[360:].sum()) for df in feats.values())
        funnel[stage] = {"passed": passed, "pct_of_scanned": passed / total_bars if total_bars else 0}
    funnel["J_L2"] = {"passed": None, "pct_of_scanned": None, "status": "NOT_MEASURED_HISTORICALLY"}

    variants = {}
    for stage in stages:
        ts = []
        for s, df in feats.items():
            ts.extend(backtest_symbol(df, s, gate_masks(df)[stage], structural=False))
        ts.sort(key=lambda t: t.entry_time)
        variants[stage] = metrics(ts)
        print(
            f"VARIANT {stage} trades={variants[stage]['trades']} "
            f"pf={variants[stage]['profit_factor']} exp={variants[stage]['expectancy_pct']}",
            flush=True,
        )

    full, by_symbol = [], {}
    for s, df in feats.items():
        t = backtest_symbol(df, s, gate_masks(df)["K_TAKER_FLOW"], structural=True)
        by_symbol[s] = metrics(t)
        full.extend(t)
    full.sort(key=lambda t: t.entry_time)
    split_ms = int((start + (end - start) * 0.70).timestamp() * 1000)
    ins = [t for t in full if t.entry_time < split_ms]
    oos = [t for t in full if t.entry_time >= split_ms]
    holdout = set(args.symbols[-3:])
    seen = [t for t in full if t.symbol not in holdout]
    unseen = [t for t in full if t.symbol in holdout]

    deltas, prev = [], None
    for st in stages:
        m = variants[st]
        if prev is None:
            deltas.append({"stage": st, "delta_trades": None, "delta_win_rate": None, "delta_expectancy_pct": None, "delta_profit_factor": None, "delta_max_drawdown_pct": None, "delta_net_return_sum_pct": None})
        else:
            pm = variants[prev]
            def d(a, b):
                if a is None or b is None:
                    return None
                if isinstance(a, float) and not np.isfinite(a):
                    return None
                if isinstance(b, float) and not np.isfinite(b):
                    return None
                return a - b
            deltas.append({
                "stage": st,
                "delta_trades": m["trades"] - pm["trades"],
                "delta_win_rate": d(m["win_rate"], pm["win_rate"]),
                "delta_expectancy_pct": d(m["expectancy_pct"], pm["expectancy_pct"]),
                "delta_profit_factor": d(m["profit_factor"], pm["profit_factor"]),
                "delta_max_drawdown_pct": d(m["max_drawdown_pct"], pm["max_drawdown_pct"]),
                "delta_net_return_sum_pct": d(m["net_return_sum_pct"], pm["net_return_sum_pct"]),
            })
        prev = st

    killers, prev_count = [], total_bars
    for st in stages:
        c = funnel[st]["passed"]
        drop = prev_count - c
        killers.append({"stage": st, "drop": drop, "drop_pct_of_previous": drop / prev_count if prev_count else 0})
        prev_count = c
    killers = sorted(killers, key=lambda x: x["drop"], reverse=True)[:5]

    fullm, oosm, unseenm = metrics(full), metrics(oos), metrics(unseen)
    start_balance, risk_usdt = 20.08, 0.20
    streak_scenarios = {
        str(k): {
            "loss_usdt": risk_usdt * k,
            "ending_balance_usdt": start_balance - risk_usdt * k,
            "drawdown_pct": risk_usdt * k / start_balance,
        }
        for k in (5, 10, 20)
    }
    result = {
        "strategy": "TST_ALLIGATOR_SMC_V2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "symbols": args.symbols,
        "cost_model": {
            "fee_per_side": FEE_SIDE,
            "spread_slippage_buffer_per_side": EXECUTION_SIDE,
            "round_trip_total_before_stop_gap": 2 * (FEE_SIDE + EXECUTION_SIDE),
        },
        "scope_warning": "Historical test includes candle-derived gates through taker flow, but NOT historical L2 bid share, live spread, visible depth, fill failures, or network latency. It cannot authorize live execution.",
        "funnel": funnel,
        "top_frequency_killers": killers,
        "ablation_common_exit": variants,
        "ablation_deltas": deltas,
        "full_minus_l2": {
            "all": fullm,
            "in_sample_70pct": metrics(ins),
            "oos_30pct": oosm,
            "seen_symbols": metrics(seen),
            "unseen_symbol_holdout": unseenm,
            "by_symbol": by_symbol,
        },
        "trade_frequency": {
            "trades_per_day": fullm["trades"] / args.days,
            "trades_per_week": fullm["trades"] / args.days * 7,
            "trades_per_month_30d": fullm["trades"] / args.days * 30,
        },
        "risk_streak_scenarios": streak_scenarios,
        "not_measured": [
            "historical L2 order-book bid-share gate",
            "historical visible depth gate",
            "historical live spread gate",
            "failed fills",
            "network latency",
            "production forward sample for this exact revision",
        ],
    }
    if fullm["trades"] < 30 or oosm["trades"] < 20:
        verdict = "INSUFFICIENT_DATA"
    elif (oosm["expectancy_pct"] or -1) > 0 and (oosm["profit_factor"] or 0) > 1.15 and (unseenm["expectancy_pct"] or -1) > 0:
        verdict = "PROMISING_NOT_LIVE_READY"
    else:
        verdict = "NO_RELIABLE_EDGE_DEMONSTRATED"
    result["verdict"] = verdict
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print("AUDIT_RESULT_JSON=" + json.dumps({
        "verdict": verdict,
        "full": fullm,
        "oos": oosm,
        "unseen": unseenm,
        "frequency": result["trade_frequency"],
        "killers": killers,
    }), flush=True)
    print(f"WROTE {out}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--output", default="validation/alligator_smc_v2/audit-latest.json")
    run(p.parse_args())
