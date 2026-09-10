#!/usr/bin/env python3
from __future__ import annotations

"""Research-only candidate discovery for the Spot Sniper historical EV replay.

Uses the same Binance Vision loader/feature primitives as historical_ev_replay.py,
but treats the current rule set as a baseline and tests a small pre-registered set
of strictly stronger filters. Selection uses only early non-holdout data. The
selected variant is then frozen and evaluated on later chronological OOS data,
unseen symbols, and walk-forward folds. This file never changes live execution.
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

OUT = ROOT / "freqtrade" / "user_data" / "historical_ev_candidate_discovery.json"
MIN_DISCOVERY_N = 100
MIN_OOS_N = 80
MIN_HOLDOUT_N = 60

VARIANTS = {
    "BASELINE": {},
    "BULL_REGIME": {"bull_regime": True},
    "RECLAIM_ONLY": {"reclaim_only": True},
    "BULL_RECLAIM": {"bull_regime": True, "reclaim_only": True},
    "BULL_RECLAIM_TAKER60": {"bull_regime": True, "reclaim_only": True, "min_taker": 0.60},
    "BULL_RECLAIM_TAKER62_VOL18": {
        "bull_regime": True, "reclaim_only": True, "min_taker": 0.62, "min_volx": 1.80
    },
    "BULL_RECLAIM_RS_POS": {
        "bull_regime": True, "reclaim_only": True, "min_rs_btc4h": 0.0
    },
    "BULL_RECLAIM_SCORE95": {
        "bull_regime": True, "reclaim_only": True, "min_score": 95
    },
    "STRONG_BULL_RECLAIM": {"strong_bull": True, "reclaim_only": True},
    "BULL_RECLAIM_MAX12PCT": {
        "bull_regime": True, "reclaim_only": True, "max_r24": 0.12
    },
}

BULL = {"STRONG_BULL", "WEAK_BULL", "POST_CRASH_RECOVERY"}


def _holdout_symbol(symbol: str) -> bool:
    x = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    return x % 4 == 0


def _passes_variant(row: dict, cfg: dict) -> bool:
    if cfg.get("bull_regime") and row.get("regime") not in BULL:
        return False
    if cfg.get("strong_bull") and row.get("regime") != "STRONG_BULL":
        return False
    if cfg.get("reclaim_only") and not bool(row.get("reclaim")):
        return False
    if float(row.get("taker_buy_share") or 0.0) < float(cfg.get("min_taker", 0.56)):
        return False
    if float(row.get("volume_expansion") or 0.0) < float(cfg.get("min_volx", 1.50)):
        return False
    if float(row.get("score") or 0.0) < float(cfg.get("min_score", 86)):
        return False
    if float(row.get("rs_vs_btc_4h") or -999.0) < float(cfg.get("min_rs_btc4h", -999.0)):
        return False
    if float(row.get("r24") or 0.0) > float(cfg.get("max_r24", 2.00)):
        return False
    return True


def _metric(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "pass": False}
    vals = [float(r["sim_net_pct"]) - h.EXTRA_STRESS_PCT for r in rows]
    mean = sum(vals) / len(vals)
    med = statistics.median(vals)
    hit = sum(v > 0 for v in vals) / len(vals)
    gross_pos = sum(v for v in vals if v > 0)
    gross_neg = -sum(v for v in vals if v < 0)
    pf = gross_pos / gross_neg if gross_neg > 1e-12 else (999.0 if gross_pos > 0 else 0.0)
    mfe = statistics.median(max(0.0, float(r.get("mfe_pct") or 0.0)) for r in rows)
    mae = statistics.median(abs(min(0.0, float(r.get("mae_pct") or 0.0))) for r in rows)
    ratio = mfe / mae if mae > 1e-12 else (999.0 if mfe > 0 else 0.0)
    stop = sum(1 for r in rows if r.get("path_outcome") == "SL") / len(rows)
    tp = sum(1 for r in rows if r.get("path_outcome") == "TP") / len(rows)
    timeout = 1.0 - stop - tp
    ci = m._bootstrap_mean_ci(vals)
    lo = ci[0]
    passed = (
        len(rows) >= MIN_OOS_N
        and mean > 0
        and med > -0.05
        and hit >= 0.50
        and pf >= 1.10
        and lo is not None
        and lo > -0.10
        and ratio >= 1.20
    )
    return {
        "n": len(rows),
        "mean_stressed_net_pct": round(mean, 4),
        "median_stressed_net_pct": round(med, 4),
        "positive_rate_stressed": round(hit, 4),
        "profit_factor_stressed": round(pf, 4),
        "bootstrap95": ci,
        "median_mfe_pct": round(mfe, 4),
        "median_abs_mae_pct": round(mae, 4),
        "median_mfe_mae_ratio": round(ratio, 4),
        "tp_rate": round(tp, 4),
        "stop_rate": round(stop, 4),
        "timeout_rate": round(max(0.0, timeout), 4),
        "pass": passed,
    }


def _discovery_metric(rows: list[dict]) -> dict:
    x = _metric(rows)
    if x.get("n", 0) < MIN_DISCOVERY_N:
        x["discovery_eligible"] = False
        return x
    x["discovery_eligible"] = (
        float(x.get("mean_stressed_net_pct") or -999) > -0.02
        and float(x.get("positive_rate_stressed") or 0) >= 0.48
        and float(x.get("profit_factor_stressed") or 0) >= 1.00
        and (x.get("bootstrap95") or [None])[0] is not None
        and float((x.get("bootstrap95") or [-999])[0]) > -0.15
    )
    return x


def _build_events() -> tuple[list[dict], dict]:
    mos = h.months()
    data: dict[str, list[dict]] = {}
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

    btc = data.get("BTCUSDT") or []
    bix = {int(b["ts"]): i for i, b in enumerate(btc)}
    idxs = {s: {int(b["ts"]): i for i, b in enumerate(bs)} for s, bs in data.items()}
    core = [set(idxs[s]) for s in h.SYMBOLS[:8] if idxs.get(s)]
    timestamps = sorted(set.intersection(*core)) if btc and core else []
    events = []
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
            s: 0.25 * ranks["r1h"][s]
            + 0.20 * ranks["r4h"][s]
            + 0.20 * ranks["rs_vs_btc_4h"][s]
            + 0.15 * ranks["accel1h"][s]
            + 0.10 * ranks["volume_expansion"][s]
            + 0.10 * ranks["liquidity"][s]
            for s in raw
        }
        opp_pct = h.pct_rank(opp)
        order = sorted(opp, key=lambda s: (opp[s], s), reverse=True)
        opp_rank = {s: i + 1 for i, s in enumerate(order)}
        bf = raw["BTCUSDT"][1]
        rg = h.regime(bf["btc1h"], bf["btc4h"], b1, b4)

        for sym, (i, fr) in raw.items():
            if sym == "BTCUSDT":
                continue
            if (
                fr["score"] < 86
                or fr["r1h"] <= 0
                or fr["r4h"] <= 0
                or fr["taker_buy_share"] < 0.56
                or fr["volume_expansion"] < 1.50
                or not (fr["breakout"] or fr["reclaim"])
                or opp_rank[sym] > h.TOP_N
                or fr["r24"] > 2.00
            ):
                continue
            if sym in last and i - last[sym] < h.MIN_GAP_BARS:
                continue
            row = {
                "event_id": f"DISC-{sym}-{ts}",
                "source_ts": ts,
                "ts": ts,
                "symbol": sym,
                "lane": h.lane(fr["r24"]),
                "regime": rg,
                "score": fr["score"],
                "risk_pct": fr["risk_pct"],
                "reward_pct": fr["reward_pct"],
                "r24": fr["r24"],
                "r1h": fr["r1h"],
                "r4h": fr["r4h"],
                "taker_buy_share": fr["taker_buy_share"],
                "volume_expansion": fr["volume_expansion"],
                "breakout": bool(fr["breakout"]),
                "reclaim": bool(fr["reclaim"]),
                "breadth_1h": b1,
                "breadth_4h": b4,
                "rs_vs_btc_4h": fr["rs_vs_btc_4h"],
                "r1h_pct_rank": ranks["r1h"][sym],
                "r4h_pct_rank": ranks["r4h"][sym],
                "rs_btc4h_pct_rank": ranks["rs_vs_btc_4h"][sym],
                "accel1h_pct_rank": ranks["accel1h"][sym],
                "volume_expansion_pct_rank": ranks["volume_expansion"][sym],
                "liquidity_pct_rank": ranks["liquidity"][sym],
                "opportunity_pct_shadow": opp_pct[sym],
                "opportunity_rank": opp_rank[sym],
                "risk_reward": {"risk": fr["risk_pct"], "reward": fr["reward_pct"]},
            }
            o = h.outcome(data[sym], i, row)
            if o:
                events.append(o)
                last[sym] = i

    events.sort(key=lambda r: (r["source_ts"], r["symbol"]))
    meta = {
        "months": mos,
        "symbols_with_data": [s for s in h.SYMBOLS if data.get(s)],
        "download_failures": failures,
        "baseline_events": len(events),
    }
    return events, meta


def _lane_metrics(rows: list[dict]) -> dict:
    return {lane: _metric([r for r in rows if r.get("lane") == lane]) for lane in ("NORMAL", "MID", "EXPLOSIVE", "EXTREME")}


def _regime_metrics(rows: list[dict]) -> dict:
    vals = sorted({str(r.get("regime") or "") for r in rows})
    return {rg: _metric([r for r in rows if r.get("regime") == rg]) for rg in vals}


def main():
    events, meta = _build_events()
    if not events:
        payload = {"version": 1, "authorization": "RESEARCH_ONLY", "liveTrading": False, "generated_at": time.time(), "status": "NO_EVENTS", **meta}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(json.dumps({"kind": "historical_ev_candidate_discovery", "status": "NO_EVENTS"}))
        return

    non_holdout = [r for r in events if not _holdout_symbol(str(r["symbol"]))]
    holdout = [r for r in events if _holdout_symbol(str(r["symbol"]))]
    cutoff_idx = max(1, int(len(non_holdout) * 0.60))
    cutoff_ts = non_holdout[cutoff_idx - 1]["source_ts"]
    discovery = [r for r in non_holdout if r["source_ts"] <= cutoff_ts]
    chrono_oos = [r for r in non_holdout if r["source_ts"] > cutoff_ts]

    discovery_results = {}
    eligible = []
    for name, cfg in VARIANTS.items():
        sub = [r for r in discovery if _passes_variant(r, cfg)]
        metric = _discovery_metric(sub)
        discovery_results[name] = metric
        if metric.get("discovery_eligible"):
            eligible.append((float(metric["mean_stressed_net_pct"]), float(metric["profit_factor_stressed"]), name))

    selected = sorted(eligible, reverse=True)[0][2] if eligible else None
    selected_cfg = VARIANTS.get(selected, {}) if selected else {}
    oos_rows = [r for r in chrono_oos if selected and _passes_variant(r, selected_cfg)]
    hold_rows = [r for r in holdout if selected and _passes_variant(r, selected_cfg)]

    wf = []
    if selected:
        n = len(non_holdout)
        initial = max(200, int(n * 0.40))
        step = max(80, (n - initial) // 3)
        cur = initial
        while cur < n and len(wf) < 3:
            end = n if len(wf) == 2 else min(n, cur + step)
            test = [r for r in non_holdout[cur:end] if _passes_variant(r, selected_cfg)]
            mm = _metric(test)
            mm.update({"start_ts": non_holdout[cur]["source_ts"] if cur < n else None, "end_ts": non_holdout[end - 1]["source_ts"] if end - 1 < n else None})
            wf.append(mm)
            cur = end

    oos_metric = _metric(oos_rows)
    hold_metric = _metric(hold_rows)
    hold_symbols = sorted({str(r["symbol"]) for r in hold_rows})
    hold_ok = hold_metric.get("n", 0) >= MIN_HOLDOUT_N and len(hold_symbols) >= 3 and bool(hold_metric.get("pass"))
    wf_ok = len(wf) == 3 and all(bool(x.get("pass")) for x in wf)
    approved = bool(selected) and bool(oos_metric.get("pass")) and hold_ok and wf_ok
    selected_rows = [r for r in events if selected and _passes_variant(r, selected_cfg)]

    payload = {
        "version": 1,
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "generated_at": time.time(),
        "source": "Binance Vision Spot monthly 15m via historical_ev_replay.py",
        "status": "APPROVED_FOR_FORWARD_SHADOW" if approved else ("NO_DISCOVERY_WINNER" if not selected else "NOT_APPROVED"),
        "baseline_ruleset_hash": h.RULESET_HASH,
        "selection_policy": "pre_registered_stricter_subsets; select on first 60% non-holdout only; freeze; validate chrono OOS + unseen symbols + 3 walk-forward folds",
        "variant_definitions": VARIANTS,
        "discovery_cutoff_ts": cutoff_ts,
        "selected_variant": selected,
        "selected_rules": selected_cfg,
        "discovery_results": discovery_results,
        "chronological_oos": oos_metric,
        "unseen_symbol_holdout": {**hold_metric, "symbols": hold_symbols},
        "walk_forward_folds": wf,
        "walk_forward_pass": wf_ok,
        "unseen_symbol_holdout_pass": hold_ok,
        "evidence_pass": approved,
        "selected_all_history": _metric(selected_rows) if selected else {"n": 0, "pass": False},
        "selected_lane_metrics": _lane_metrics(selected_rows) if selected else {},
        "selected_regime_metrics": _regime_metrics(selected_rows) if selected else {},
        **meta,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps({"kind": "historical_ev_candidate_discovery", "status": payload["status"], "selected": selected, "oos": oos_metric, "holdout": payload["unseen_symbol_holdout"], "wf_pass": wf_ok}, separators=(",", ":")))


if __name__ == "__main__":
    main()
