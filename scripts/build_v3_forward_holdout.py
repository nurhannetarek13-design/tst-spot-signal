#!/usr/bin/env python3
"""Build prospective V3 PAPER holdout metrics from frozen runtime evidence.

The holdout is considered untouched only when the active config bytes still
match the Git blob frozen in policy. Trades before freezeAt are ignored.
This script never enables live trading.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import subprocess
from datetime import datetime, timezone


def _dt(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _git_blob_sha(path: pathlib.Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "hash-object", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _profit_factor(pnls):
    gross_win=sum(x for x in pnls if x>0)
    gross_loss=abs(sum(x for x in pnls if x<0))
    if gross_loss<=0:
        return 99.0 if gross_win>0 else 0.0
    return gross_win/gross_loss


def build(state, policy, config_path: pathlib.Path):
    freeze=_dt(policy["freezeAt"])
    expected_engine=str(policy["expectedEngine"])
    expected_blob=str(policy["configGitBlobSha"])
    actual_blob=_git_blob_sha(config_path)

    rows=[]
    malformed=[]
    for trade in state.get("closed_trades") or []:
        try:
            opened=_dt(trade.get("opened_at"))
        except Exception:
            malformed.append({"symbol":trade.get("symbol"),"reason":"OPENED_AT_INVALID"})
            continue
        if opened < freeze:
            continue
        rows.append(trade)

    engine_ok=bool(
        state.get("mode")=="PAPER_ONLY"
        and state.get("engine")==expected_engine
        and all(str(x.get("engine"))==expected_engine for x in rows)
    )

    missing_cost_fields=[]
    for x in rows:
        ctx=x.get("entry_context") or {}
        if ctx.get("estimated_entry_slippage_bps") is None:
            missing_cost_fields.append({"symbol":x.get("symbol"),"opened_at":x.get("opened_at"),"field":"entry_slippage"})
        if x.get("exit_slippage_bps") is None:
            missing_cost_fields.append({"symbol":x.get("symbol"),"opened_at":x.get("opened_at"),"field":"exit_slippage"})

    config_unchanged=bool(actual_blob and actual_blob==expected_blob)
    untouched=bool(
        policy.get("frozen") is True
        and config_unchanged
        and engine_ok
        and not malformed
    )
    costs_included=bool(
        not missing_cost_fields
        and policy.get("requireExecutionCostFields") is True
    )

    pnls=[float(x.get("pnl_usdt") or 0.0) for x in rows]
    wins=sum(x>0 for x in pnls)
    losses=sum(x<0 for x in pnls)
    net=sum(pnls)
    trades=len(rows)
    win_rate=wins/trades if trades else 0.0
    expectancy=net/trades if trades else 0.0
    pf=_profit_factor(pnls)

    target_n=int(policy.get("minimumTradesForClaim") or 100)
    target_wr=float(policy.get("minimumWinRateForClaim") or .99)
    require_positive=bool(policy.get("requirePositiveNet",True))

    blockers=[]
    if not untouched: blockers.append("FORWARD_HOLDOUT_NOT_UNTOUCHED")
    if not costs_included: blockers.append("FORWARD_COST_FIELDS_INCOMPLETE")
    if trades<target_n: blockers.append("FORWARD_SAMPLE_TOO_SMALL")
    if win_rate<target_wr: blockers.append("FORWARD_WIN_RATE_BELOW_TARGET")
    if require_positive and net<=0: blockers.append("FORWARD_NET_NOT_POSITIVE")

    return {
        "engine":"INDICATOR_V3_FORWARD_HOLDOUT_METRICS_V1",
        "authorization":"PAPER_EVIDENCE_ONLY",
        "liveTrading":False,
        "freezeAt":policy["freezeAt"],
        "expectedEngine":expected_engine,
        "configGitBlobShaExpected":expected_blob,
        "configGitBlobShaActual":actual_blob,
        "configUnchanged":config_unchanged,
        "untouched":untouched,
        "costsIncluded":costs_included,
        "trades":trades,
        "wins":wins,
        "losses":losses,
        "winRate":win_rate,
        "netPnlUSDT":net,
        "netPnlPerUnit":net,
        "expectancyUSDT":expectancy,
        "expectancyPerUnit":expectancy,
        "profitFactor":pf,
        "sampleSymbols":sorted({str(x.get("symbol")) for x in rows if x.get("symbol")}),
        "missingCostFields":missing_cost_fields,
        "malformedTrades":malformed,
        "claimReady":not blockers,
        "claimBlockedReasons":blockers,
        "note":"Prospective PAPER evidence only. claimReady never enables live trading by itself.",
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--state",default="ready_bot/indicator_state.json")
    ap.add_argument("--policy",default="validation/indicator-v3/forward-holdout-policy.json")
    ap.add_argument("--config",default="ready_bot/indicator_config.json")
    ap.add_argument("--output",default="validation/indicator-v3/holdout-metrics.json")
    args=ap.parse_args()
    state=json.loads(pathlib.Path(args.state).read_text())
    policy=json.loads(pathlib.Path(args.policy).read_text())
    out=build(state,policy,pathlib.Path(args.config))
    p=pathlib.Path(args.output);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(out,indent=2,sort_keys=True))
    print(json.dumps(out,indent=2,sort_keys=True))


if __name__=="__main__":
    main()
