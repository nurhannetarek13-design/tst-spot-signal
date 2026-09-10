#!/usr/bin/env python3
from __future__ import annotations

"""Research-only Spot Sniper retest-entry discovery.

The existing momentum signal is treated as SETUP ONLY. No entry is taken on the
setup candle. We wait for a point-in-time pullback/retest and a later reclaim
confirmation, then enter at that confirmation close. Candidate definitions are
pre-registered below. Selection uses only early non-holdout data; the selected
variant is frozen before chronological OOS, unseen-symbol and walk-forward tests.
No live execution state is modified by this program.
"""

import hashlib
import json
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "freqtrade"))

import historical_ev_replay as h
import shadow_ev_model as m

OUT = ROOT / "freqtrade" / "user_data" / "historical_retest_entry_discovery.json"

# Fixed evidence rules. These are intentionally not tuned after OOS is seen.
MIN_DISCOVERY_N = 80
MIN_OOS_N = 50
MIN_HOLDOUT_N = 35
HORIZON_BARS = 8  # 2h after confirmed entry on 15m bars
BULL = {"STRONG_BULL", "WEAK_BULL", "POST_CRASH_RECOVERY"}

# The setup candle is never an entry. Each variant controls the subsequent
# pullback/reclaim geometry and exit geometry. All thresholds are pre-registered.
VARIANTS = {
    "RETEST_A": {
        "wait_bars": 4, "pullback_min": 0.0035, "pullback_max": 0.018,
        "reclaim_level_frac": 0.994, "entry_taker_min": 0.52,
        "entry_volx_min": 0.75, "bull_only": False, "tp_pct": 0.014, "sl_pct": 0.0065,
    },
    "RETEST_B_BULL": {
        "wait_bars": 4, "pullback_min": 0.0035, "pullback_max": 0.018,
        "reclaim_level_frac": 0.994, "entry_taker_min": 0.52,
        "entry_volx_min": 0.75, "bull_only": True, "tp_pct": 0.014, "sl_pct": 0.0065,
    },
    "RETEST_C_DEEPER": {
        "wait_bars": 6, "pullback_min": 0.0060, "pullback_max": 0.025,
        "reclaim_level_frac": 0.992, "entry_taker_min": 0.52,
        "entry_volx_min": 0.70, "bull_only": False, "tp_pct": 0.015, "sl_pct": 0.0070,
    },
    "RETEST_D_FLOW": {
        "wait_bars": 4, "pullback_min": 0.0035, "pullback_max": 0.018,
        "reclaim_level_frac": 0.995, "entry_taker_min": 0.56,
        "entry_volx_min": 0.90, "bull_only": False, "tp_pct": 0.014, "sl_pct": 0.0065,
    },
    "RETEST_E_BULL_FLOW": {
        "wait_bars": 4, "pullback_min": 0.0035, "pullback_max": 0.018,
        "reclaim_level_frac": 0.995, "entry_taker_min": 0.56,
        "entry_volx_min": 0.90, "bull_only": True, "tp_pct": 0.014, "sl_pct": 0.0065,
    },
    "RETEST_F_TIGHT": {
        "wait_bars": 4, "pullback_min": 0.0025, "pullback_max": 0.012,
        "reclaim_level_frac": 0.996, "entry_taker_min": 0.54,
        "entry_volx_min": 0.80, "bull_only": False, "tp_pct": 0.012, "sl_pct": 0.0055,
    },
    "RETEST_G_WIDE_RR": {
        "wait_bars": 6, "pullback_min": 0.0040, "pullback_max": 0.022,
        "reclaim_level_frac": 0.993, "entry_taker_min": 0.52,
        "entry_volx_min": 0.70, "bull_only": False, "tp_pct": 0.018, "sl_pct": 0.0075,
    },
}


def holdout_symbol(symbol: str) -> bool:
    # Deterministic 25% symbol holdout, never used for selection.
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16) % 4 == 0


def median(xs):
    return statistics.median(xs) if xs else 0.0


def load_data():
    mos = h.months()
    data = {}
    failures = []
    for sym in h.SYMBOLS:
        bars = []
        for mo in mos:
            try:
                bars.extend(h.parse(h.download(sym, mo)))
            except Exception as exc:
                failures.append({"symbol": sym, "month": mo, "error": f"{type(exc).__name__}:{str(exc)[:120]}"})
        ded = {int(b["ts"]): b for b in bars}
        data[sym] = [ded[k] for k in sorted(ded)]
    return mos, data, failures


def setup_events(data):
    """Rebuild the current scanner as SETUP events only, not entries."""
    btc = data.get("BTCUSDT") or []
    bix = {int(b["ts"]): i for i, b in enumerate(btc)}
    idxs = {s: {int(b["ts"]): i for i, b in enumerate(bs)} for s, bs in data.items()}
    core = [set(idxs[s]) for s in h.SYMBOLS[:8] if idxs.get(s)]
    timestamps = sorted(set.intersection(*core)) if btc and core else []
    setups = []
    last = {}

    for ts in timestamps:
        bi = bix.get(ts)
        if bi is None or bi < 100:
            continue
        raw = {}
        for sym, bars in data.items():
            i = idxs[sym].get(ts)
            fr = h.raw_feature(bars, i, btc, bi) if i is not None else None
            if fr is not None:
                raw[sym] = (i, fr)
        if len(raw) < 10 or "BTCUSDT" not in raw:
            continue

        b1 = sum(1 for _, fr in raw.values() if fr["r1h"] > 0) / len(raw)
        b4 = sum(1 for _, fr in raw.values() if fr["r4h"] > 0) / len(raw)
        ranks = {
            k: h.pct_rank({s: fr[k] for s, (_, fr) in raw.items()})
            for k in ("r1h", "r4h", "rs_vs_btc_4h", "accel1h", "volume_expansion", "liquidity")
        }
        opp = {
            s: .25*ranks["r1h"][s] + .20*ranks["r4h"][s] + .20*ranks["rs_vs_btc_4h"][s]
               + .15*ranks["accel1h"][s] + .10*ranks["volume_expansion"][s] + .10*ranks["liquidity"][s]
            for s in raw
        }
        order = sorted(opp, key=lambda s: (opp[s], s), reverse=True)
        opp_rank = {s: j + 1 for j, s in enumerate(order)}
        bf = raw["BTCUSDT"][1]
        rg = h.regime(bf["btc1h"], bf["btc4h"], b1, b4)

        for sym, (i, fr) in raw.items():
            if sym == "BTCUSDT":
                continue
            if (
                fr["score"] < 86 or fr["r1h"] <= 0 or fr["r4h"] <= 0
                or fr["taker_buy_share"] < .56 or fr["volume_expansion"] < 1.50
                or not (fr["breakout"] or fr["reclaim"])
                or opp_rank[sym] > h.TOP_N or fr["r24"] > 2.00
            ):
                continue
            if sym in last and i - last[sym] < h.MIN_GAP_BARS:
                continue
            bars = data[sym]
            if i < 9 or i + 16 >= len(bars):
                continue
            # All levels below are known at setup close.
            pre_high = max(x["high"] for x in bars[i-8:i])
            recent_low = min(x["low"] for x in bars[i-8:i+1])
            setups.append({
                "symbol": sym, "i": i, "ts": ts, "setup_close": bars[i]["close"],
                "pre_high": pre_high, "recent_low": recent_low,
                "regime": rg, "score": fr["score"], "r24": fr["r24"],
                "setup_taker": fr["taker_buy_share"], "setup_volx": fr["volume_expansion"],
                "rs_vs_btc_4h": fr["rs_vs_btc_4h"], "rank": opp_rank[sym],
                "lane": h.lane(fr["r24"]),
            })
            last[sym] = i
    return setups


def confirm_entry(bars, setup, cfg):
    """Point-in-time retest confirmation after setup. Returns entry bar index or None."""
    i = setup["i"]
    setup_close = float(setup["setup_close"])
    level = float(setup["pre_high"])
    if cfg.get("bull_only") and setup["regime"] not in BULL:
        return None

    pulled = False
    pull_low = None
    end = min(len(bars)-HORIZON_BARS-1, i + int(cfg["wait_bars"]))
    for j in range(i + 1, end + 1):
        b = bars[j]
        dd = max(0.0, 1.0 - float(b["low"]) / setup_close)
        if float(cfg["pullback_min"]) <= dd <= float(cfg["pullback_max"]):
            pulled = True
            pull_low = float(b["low"]) if pull_low is None else min(pull_low, float(b["low"]))
        if not pulled:
            continue

        # Confirmation uses only the current/previous completed bars.
        q_hist = [float(x["quote_volume"]) for x in bars[max(0, j-20):j]]
        medq = median(q_hist)
        volx = float(b["quote_volume"]) / medq if medq > 0 else 0.0
        taker = float(b["taker_buy_quote"]) / float(b["quote_volume"]) if float(b["quote_volume"]) > 0 else 0.0
        green = float(b["close"]) > float(b["open"])
        improving = float(b["close"]) > float(bars[j-1]["close"])
        reclaim = float(b["close"]) >= level * float(cfg["reclaim_level_frac"])
        no_chase = float(b["close"]) <= setup_close * 1.004
        if green and improving and reclaim and no_chase and taker >= float(cfg["entry_taker_min"]) and volx >= float(cfg["entry_volx_min"]):
            return j, {"entry_taker": taker, "entry_volx": volx, "pull_low": pull_low, "pullback_from_setup": 1.0 - float(b["close"]) / setup_close}
    return None


def outcome(bars, j, setup, cfg, extra):
    entry = float(bars[j]["close"])
    tp = entry * (1.0 + float(cfg["tp_pct"]))
    sl = entry * (1.0 - float(cfg["sl_pct"]))
    future = bars[j+1:j+1+HORIZON_BARS]
    if len(future) < HORIZON_BARS:
        return None
    path = None
    hit_bar = None
    for k, b in enumerate(future, 1):
        hit_tp = float(b["high"]) >= tp
        hit_sl = float(b["low"]) <= sl
        if hit_tp and hit_sl:
            path, hit_bar = "SL", k  # adverse intrabar ordering
            break
        if hit_sl:
            path, hit_bar = "SL", k
            break
        if hit_tp:
            path, hit_bar = "TP", k
            break
    mfe = (max(float(b["high"]) for b in future) / entry - 1.0) * 100.0
    mae = (min(float(b["low"]) for b in future) / entry - 1.0) * 100.0
    if path == "TP":
        gross = float(cfg["tp_pct"]) * 100.0
    elif path == "SL":
        gross = -float(cfg["sl_pct"]) * 100.0
    else:
        gross = (float(future[-1]["close"]) / entry - 1.0) * 100.0
    return {
        **setup, **extra,
        "entry_i": j, "entry_ts": bars[j]["ts"], "entry": entry,
        "target": tp, "stop": sl, "path_outcome": path,
        "holding_min": hit_bar * 15 if hit_bar else HORIZON_BARS * 15,
        "mfe_pct": mfe, "mae_pct": mae,
        "sim_gross_pct": gross, "sim_net_pct": gross - h.COST_PCT,
        "ret60_pct": (float(bars[j+4]["close"]) / entry - 1.0) * 100.0,
        "ret120_pct": (float(bars[j+8]["close"]) / entry - 1.0) * 100.0,
    }


def metric(rows, min_n=MIN_OOS_N):
    if not rows:
        return {"n": 0, "pass": False}
    vals = [float(r["sim_net_pct"]) - h.EXTRA_STRESS_PCT for r in rows]
    mean = sum(vals) / len(vals)
    med = statistics.median(vals)
    hit = sum(v > 0 for v in vals) / len(vals)
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    pf = gp/gl if gl > 1e-12 else (999.0 if gp > 0 else 0.0)
    mfe = statistics.median(max(0.0, float(r["mfe_pct"])) for r in rows)
    mae = statistics.median(abs(min(0.0, float(r["mae_pct"]))) for r in rows)
    ratio = mfe/mae if mae > 1e-12 else (999.0 if mfe > 0 else 0.0)
    ret60 = statistics.median(float(r["ret60_pct"]) for r in rows)
    ret120 = statistics.median(float(r["ret120_pct"]) for r in rows)
    ci = m._bootstrap_mean_ci(vals)
    lo = ci[0]
    passed = (
        len(rows) >= min_n and mean > 0 and med > -0.05 and hit >= .50
        and pf >= 1.10 and ratio >= 1.20 and lo is not None and lo > -.10
        and ret120 > 0
    )
    return {
        "n": len(rows), "mean_stressed_net_pct": round(mean,4),
        "median_stressed_net_pct": round(med,4), "positive_rate_stressed": round(hit,4),
        "profit_factor_stressed": round(pf,4), "bootstrap95": ci,
        "median_mfe_pct": round(mfe,4), "median_abs_mae_pct": round(mae,4),
        "median_mfe_mae_ratio": round(ratio,4), "median_ret60_pct": round(ret60,4),
        "median_ret120_pct": round(ret120,4),
        "tp_rate": round(sum(r["path_outcome"]=="TP" for r in rows)/len(rows),4),
        "stop_rate": round(sum(r["path_outcome"]=="SL" for r in rows)/len(rows),4),
        "pass": passed,
    }


def build_variant_rows(data, setups, cfg):
    out = []
    last_entry = {}
    for s in setups:
        bars = data[s["symbol"]]
        got = confirm_entry(bars, s, cfg)
        if not got:
            continue
        j, extra = got
        if s["symbol"] in last_entry and j - last_entry[s["symbol"]] < h.MIN_GAP_BARS:
            continue
        row = outcome(bars, j, s, cfg, extra)
        if row:
            out.append(row)
            last_entry[s["symbol"]] = j
    out.sort(key=lambda r: (r["entry_ts"], r["symbol"]))
    return out


def main():
    mos, data, failures = load_data()
    setups = setup_events(data)
    non_hold_setups = [s for s in setups if not holdout_symbol(s["symbol"])]
    if not non_hold_setups:
        payload = {"authorization":"RESEARCH_ONLY","liveTrading":False,"status":"NO_SETUPS","evidence_pass":False}
        OUT.parent.mkdir(parents=True, exist_ok=True); OUT.write_text(json.dumps(payload,indent=2)); return

    cutoff = non_hold_setups[max(1,int(len(non_hold_setups)*.60))-1]["ts"]
    discovery_setups = [s for s in non_hold_setups if s["ts"] <= cutoff]
    oos_setups = [s for s in non_hold_setups if s["ts"] > cutoff]
    hold_setups = [s for s in setups if holdout_symbol(s["symbol"])]

    discovery_results = {}
    eligible = []
    cached = {}
    for name,cfg in VARIANTS.items():
        rows = build_variant_rows(data, discovery_setups, cfg)
        cached[name] = rows
        mm = metric(rows, min_n=MIN_DISCOVERY_N)
        discovery_results[name] = mm
        if mm.get("pass"):
            # Prefer strong mean, then PF, then larger sample.
            eligible.append((float(mm["mean_stressed_net_pct"]), float(mm["profit_factor_stressed"]), int(mm["n"]), name))

    selected = sorted(eligible, reverse=True)[0][3] if eligible else None
    cfg = VARIANTS.get(selected) if selected else None
    oos_rows = build_variant_rows(data, oos_setups, cfg) if cfg else []
    hold_rows = build_variant_rows(data, hold_setups, cfg) if cfg else []
    oos_m = metric(oos_rows)
    hold_m = metric(hold_rows, min_n=MIN_HOLDOUT_N)
    hold_syms = sorted({r["symbol"] for r in hold_rows})
    hold_ok = bool(hold_m.get("pass")) and len(hold_syms) >= 3

    wf=[]
    if cfg:
        ns=non_hold_setups
        initial=max(160,int(len(ns)*.40)); step=max(80,(len(ns)-initial)//3); cur=initial
        while cur<len(ns) and len(wf)<3:
            end=len(ns) if len(wf)==2 else min(len(ns),cur+step)
            rr=build_variant_rows(data,ns[cur:end],cfg)
            mm=metric(rr,min_n=35)
            mm.update({"setup_start_ts":ns[cur]["ts"] if cur<len(ns) else None,"setup_end_ts":ns[end-1]["ts"]})
            wf.append(mm); cur=end
    wf_ok=len(wf)==3 and all(bool(x.get("pass")) for x in wf)
    approved=bool(selected) and bool(oos_m.get("pass")) and hold_ok and wf_ok

    all_rows=build_variant_rows(data,setups,cfg) if cfg else []
    payload={
        "version":1,"authorization":"RESEARCH_ONLY","liveTrading":False,
        "generated_at":time.time(),"source":"Binance Vision Spot monthly 15m",
        "months":mos,"download_failures":failures,"baseline_setup_count":len(setups),
        "selection_policy":"setup candle never entered; pre-registered retest variants; select on first 60% non-holdout; freeze; chrono OOS + unseen symbols + 3 WF",
        "discovery_cutoff_ts":cutoff,"variant_definitions":VARIANTS,
        "discovery_results":discovery_results,"selected_variant":selected,
        "selected_rules":cfg or {},"chronological_oos":oos_m,
        "unseen_symbol_holdout":{**hold_m,"symbols":hold_syms},
        "unseen_symbol_holdout_pass":hold_ok,"walk_forward_folds":wf,
        "walk_forward_pass":wf_ok,"selected_all_history":metric(all_rows,min_n=1) if all_rows else {"n":0,"pass":False},
        "evidence_pass":approved,"status":"APPROVED_FOR_FORWARD_SHADOW" if approved else ("NO_DISCOVERY_WINNER" if not selected else "NOT_APPROVED"),
    }
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,indent=2,sort_keys=True))
    print(json.dumps({"kind":"historical_retest_entry_discovery","status":payload["status"],"selected":selected,"oos":oos_m,"holdout":payload["unseen_symbol_holdout"],"wf_pass":wf_ok},separators=(",",":")))

if __name__=="__main__": main()
