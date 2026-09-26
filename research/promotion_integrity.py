"""Fail-closed research promotion gate for calibrated forward edge."""
from __future__ import annotations
def promotion_gate(*, calibration, drift, fdr, baselines, dependency, forward, champion):
    reasons=[]
    groups=(calibration or {}).get("groups") or {}
    if not groups or not any(g.get("evidence_ready") for g in groups.values()): reasons.append("CALIBRATION_NOT_READY")
    if (drift or {}).get("block_promotion",True): reasons.append("FEATURE_DRIFT")
    if int((fdr or {}).get("hypotheses_tested") or 0)<=0: reasons.append("HYPOTHESIS_COUNT_MISSING")
    if int((fdr or {}).get("accepted") or 0)<=0: reasons.append("NO_FDR_SURVIVOR")
    if (baselines or {}).get("complexity_justified") is not True: reasons.append("BASELINES_NOT_BEATEN")
    if (dependency or {}).get("independence_review_required") is True and (dependency or {}).get("reviewed") is not True: reasons.append("DEPENDENCY_REVIEW_REQUIRED")
    if (forward or {}).get("evidence_pass") is not True: reasons.append("FORWARD_EVIDENCE_NOT_PASSING")
    if (champion or {}).get("shadow_pass") is not True: reasons.append("CHAMPION_CHALLENGER_NOT_PASSING")
    return {"promotion_allowed":not reasons,"reasons":reasons,"automatic_live_authorization":False}
