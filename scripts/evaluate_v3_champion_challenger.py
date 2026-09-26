#!/usr/bin/env python3
"""Evaluate V3 PAPER champion vs isolated SHADOW challenger.

Never edits active config and never authorizes live trading.
"""
from __future__ import annotations
import argparse,json,math,pathlib,statistics


def load(path):
    p=pathlib.Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def trade_metrics(state):
    rows=list(state.get("closed_trades") or [])
    pnls=[float(x.get("pnl_usdt") or 0.0) for x in rows]
    wins=sum(x for x in pnls if x>0)
    losses=-sum(x for x in pnls if x<0)
    pf=(wins/losses) if losses>0 else (99.0 if wins>0 else 0.0)
    exp=sum(pnls)/len(pnls) if pnls else 0.0
    hit=sum(x>0 for x in pnls)/len(pnls) if pnls else 0.0
    eq=peak=dd=0.0
    for x in pnls:
        eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
    mfe=[float(x["mfe_r"]) for x in rows if x.get("mfe_r") is not None]
    mae=[float(x["mae_r"]) for x in rows if x.get("mae_r") is not None]
    return {
        "closedTrades":len(rows),
        "profitFactor":pf,
        "expectancyUSDT":exp,
        "hitRate":hit,
        "maxDrawdownUSDT":dd,
        "medianMfeR":statistics.median(mfe) if mfe else None,
        "medianMaeR":statistics.median(mae) if mae else None,
    }


def median_slippage(state):
    rows=[]
    for symbol,row in (state.get("slippage_model") or {}).items():
        if row.get("ewma_bps") is not None:
            rows.extend([float(row["ewma_bps"])]*max(1,int(row.get("count") or 1)))
    return statistics.median(rows) if rows else None


def evaluate(champion,challenger,registry,replay=None,backtest_integrity=None):
    policy=registry["promotionPolicy"]
    cm=trade_metrics(champion);xm=trade_metrics(challenger)
    cslip=median_slippage(champion);xslip=median_slippage(challenger)
    replay_ok=bool(replay and replay.get("replayIntegrityPass") is True and replay.get("executionReplayPass") is True)
    backtest_ok=bool(backtest_integrity and backtest_integrity.get("productionGradeHistoricalEvidence") is True)

    reasons=[]
    if xm["closedTrades"]<int(policy["minimumClosedTrades"]):reasons.append("CHALLENGER_SAMPLE_TOO_SMALL")
    if xm["profitFactor"]<float(policy["minimumProfitFactor"]):reasons.append("CHALLENGER_PF_TOO_LOW")
    if xm["expectancyUSDT"]<=0:reasons.append("CHALLENGER_EXPECTANCY_NONPOSITIVE")
    if xm["maxDrawdownUSDT"]>float(policy["maximumDrawdownUSDT"]):reasons.append("CHALLENGER_DRAWDOWN_TOO_HIGH")
    if xslip is None or xslip>float(policy["maximumMedianEntrySlippageBps"]):reasons.append("CHALLENGER_SLIPPAGE_NOT_PROVEN")
    if policy.get("requireSpotMicrostructureReplay") and not replay_ok:reasons.append("SPOT_MICROSTRUCTURE_REPLAY_NOT_PROVEN")
    if policy.get("requireBacktestIntegrity") and not backtest_ok:reasons.append("BACKTEST_INTEGRITY_NOT_PROVEN")

    if cm["closedTrades"]>=int(policy["minimumClosedTrades"]):
        min_pf=cm["profitFactor"]*(1+float(policy["challengerMustBeatChampionProfitFactorByFraction"]))
        if xm["profitFactor"]<min_pf:reasons.append("CHALLENGER_NOT_BETTER_PF")
        max_dd=cm["maxDrawdownUSDT"]*(1+float(policy["challengerMustNotIncreaseDrawdownByFraction"]))
        if cm["maxDrawdownUSDT"]>0 and xm["maxDrawdownUSDT"]>max_dd:reasons.append("CHALLENGER_WORSE_DRAWDOWN")
    else:
        reasons.append("CHAMPION_REFERENCE_SAMPLE_TOO_SMALL")

    evidence_pass=not reasons
    return {
        "engine":"INDICATOR_V3_CHAMPION_CHALLENGER_EVALUATOR",
        "authorization":"PAPER_SHADOW_ONLY",
        "liveTrading":False,
        "automaticPromotion":False,
        "champion":cm,
        "challenger":xm,
        "championMedianSlippageBps":cslip,
        "challengerMedianSlippageBps":xslip,
        "spotReplayEvidence":replay or {"status":"MISSING"},
        "backtestIntegrityEvidence":backtest_integrity or {"status":"MISSING"},
        "evidencePass":evidence_pass,
        "promotionReadyForManualReview":evidence_pass,
        "reasons":reasons,
        "decision":"MANUAL_REVIEW_ELIGIBLE" if evidence_pass else "KEEP_CHAMPION",
        "note":"Even MANUAL_REVIEW_ELIGIBLE does not enable live trading or edit active config."
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--champion",default="ready_bot/indicator_state.json")
    ap.add_argument("--challenger",default="validation/indicator-v3/challenger-state.json")
    ap.add_argument("--registry",default="validation/indicator-v3/champion-challenger.json")
    ap.add_argument("--replay")
    ap.add_argument("--backtest-integrity")
    ap.add_argument("--output",default="validation/indicator-v3/champion-challenger-latest.json")
    a=ap.parse_args()
    replay=load(a.replay) if a.replay else None
    integrity=load(a.backtest_integrity) if a.backtest_integrity else None
    out=evaluate(load(a.champion),load(a.challenger),load(a.registry),replay,integrity)
    p=pathlib.Path(a.output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2))
    print(json.dumps(out,indent=2))


if __name__=="__main__":main()
