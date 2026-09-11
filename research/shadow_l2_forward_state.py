#!/usr/bin/env python3
"""Persistent outcome tracker for Gate G L2 shadow A/B.

Joins each base SHADOW_BUY opportunity to the frozen Gate A paper-trade id and
later copies the realized paper outcome. Treatment is counterfactual only:
confirmed trades inherit the Control outcome, vetoed/unavailable trades count
as zero PnL for Treatment. No account access and no execution.
"""
from __future__ import annotations

import csv
import json
import pathlib

ROOT = pathlib.Path("validation/pro_stack")
CURRENT = ROOT / "shadow_l2_ablation.csv"
FEATURES = ROOT / "feature_store.json"
BASE_STATE = ROOT / "state" / "shadow_trades.csv"
STATE = ROOT / "state" / "shadow_l2_ablation_state.csv"
SUMMARY = ROOT / "state" / "shadow_l2_ablation_performance.json"
EXP = pathlib.Path("research/spot_baseline_gate_a.json")

FIELDS = [
    "experiment", "trade_id", "feature_ts", "symbol", "control_decision",
    "treatment_decision", "treatment_reason", "l2_exchange", "obi10",
    "microprice_bps", "spread_bps", "p_tp_before_sl", "expected_net_pct",
    "base_status", "close_reason", "control_net_return_pct",
    "treatment_net_return_pct", "tp_before_sl", "holding_min"
]


def read_csv(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save(rows: list[dict]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> None:
    cfg = json.loads(EXP.read_text())
    experiment = cfg["experiment"]
    features = json.loads(FEATURES.read_text())["rows"]
    fmap = {r["symbol"]: r for r in features}
    current = read_csv(CURRENT)
    rows = read_csv(STATE)
    by_id = {r.get("trade_id"): r for r in rows if r.get("trade_id")}

    # Persist only opportunities the frozen base actually wanted to buy.
    added = 0
    for r in current:
        if r.get("base_decision") != "SHADOW_BUY":
            continue
        symbol = r["symbol"]
        feat = fmap[symbol]
        feature_ts = int(feat["ts"])
        trade_id = f"{experiment}:{symbol}:{feature_ts}"
        if trade_id in by_id:
            continue
        rec = {
            "experiment": experiment,
            "trade_id": trade_id,
            "feature_ts": feature_ts,
            "symbol": symbol,
            "control_decision": "SHADOW_BUY",
            "treatment_decision": r.get("treatment_decision", "SKIP"),
            "treatment_reason": r.get("treatment_reason", ""),
            "l2_exchange": r.get("l2_exchange", ""),
            "obi10": r.get("obi10", ""),
            "microprice_bps": r.get("microprice_bps", ""),
            "spread_bps": r.get("spread_bps", ""),
            "p_tp_before_sl": r.get("p_tp_before_sl", ""),
            "expected_net_pct": r.get("expected_net_pct", ""),
            "base_status": "PENDING",
            "close_reason": "",
            "control_net_return_pct": "",
            "treatment_net_return_pct": "",
            "tp_before_sl": "",
            "holding_min": "",
        }
        rows.append(rec)
        by_id[trade_id] = rec
        added += 1

    # Copy realized outcomes from the canonical frozen Gate A paper state.
    base = {r.get("trade_id"): r for r in read_csv(BASE_STATE)}
    newly_closed = 0
    for r in rows:
        b = base.get(r.get("trade_id"))
        if not b:
            # If portfolio risk blocked the paper trade, do not invent an outcome.
            if r.get("base_status") in ("", "PENDING"):
                r["base_status"] = "NOT_OPENED_BY_PORTFOLIO_GATE"
            continue
        was_closed = r.get("base_status") == "CLOSED"
        r["base_status"] = b.get("status", "")
        r["close_reason"] = b.get("close_reason", "")
        r["tp_before_sl"] = b.get("tp_before_sl", "")
        r["holding_min"] = b.get("holding_min", "")
        if b.get("status") == "CLOSED":
            net = fnum(b.get("net_return_pct"))
            if net is not None:
                r["control_net_return_pct"] = net
                r["treatment_net_return_pct"] = (
                    net if r.get("treatment_decision") == "SHADOW_BUY" else 0.0
                )
            if not was_closed:
                newly_closed += 1

    save(rows)

    closed = [r for r in rows if r.get("base_status") == "CLOSED" and fnum(r.get("control_net_return_pct")) is not None]
    confirmed = [r for r in closed if r.get("treatment_decision") == "SHADOW_BUY"]
    vetoed = [r for r in closed if r.get("treatment_reason") == "L2_VETO"]
    unavailable = [r for r in closed if r.get("treatment_reason") == "L2_UNAVAILABLE"]
    cvals = [fnum(r["control_net_return_pct"]) for r in closed]
    tvals = [fnum(r["treatment_net_return_pct"]) for r in closed]
    confirmed_vals = [fnum(r["control_net_return_pct"]) for r in confirmed]
    vetoed_vals = [fnum(r["control_net_return_pct"]) for r in vetoed]

    def mean(v):
        return sum(v) / len(v) if v else None

    avoided_losses = sum((fnum(r["control_net_return_pct"]) or 0) < 0 for r in vetoed)
    killed_winners = sum((fnum(r["control_net_return_pct"]) or 0) > 0 for r in vetoed)
    summary = {
        "engine": "GATE_G_SHADOW_AB_FORWARD_V1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "experiment": experiment,
        "opportunities": len(rows),
        "closedOpportunities": len(closed),
        "openOrPending": len(rows) - len(closed),
        "addedThisRun": added,
        "newlyClosedThisRun": newly_closed,
        "confirmedClosed": len(confirmed),
        "vetoedClosed": len(vetoed),
        "unavailableClosed": len(unavailable),
        "controlMeanNetPct": mean(cvals),
        "treatmentMeanNetPctPerOpportunity": mean(tvals),
        "confirmedMeanNetPct": mean(confirmed_vals),
        "vetoedWouldHaveMeanNetPct": mean(vetoed_vals),
        "avoidedLosses": avoided_losses,
        "killedWinners": killed_winners,
        "treatmentLiftPctPointsPerOpportunity": (mean(tvals) - mean(cvals)) if cvals else None,
        "promotionCount": sum(r.get("control_decision") != "SHADOW_BUY" and r.get("treatment_decision") == "SHADOW_BUY" for r in rows),
    }
    assert summary["promotionCount"] == 0
    SUMMARY.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
