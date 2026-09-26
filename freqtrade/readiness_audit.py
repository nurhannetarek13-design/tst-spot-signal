"""Machine-readable audit for the 20 deep readiness topics."""
from __future__ import annotations
TOPICS=[
"PROBABILITY_EV_CALIBRATION","UNCERTAINTY_ABSTENTION","REGIME_TRANSITION","DYNAMIC_PORTFOLIO_CORRELATION",
"PORTFOLIO_STRESS","MARKET_IMPACT_CAPACITY","QUEUE_POSITION","FILL_TOXICITY","EXACT_FILL_PNL_ACCOUNTING",
"DUST_ORPHAN_MANAGER","EXCHANGE_DEGRADED_MODE","DISASTER_RECOVERY","END_TO_END_LATENCY","FEATURE_DATA_DRIFT",
"SECURITY_HARDENING","SCHEMA_API_CONTRACTS","BASELINE_CONTROL","FALSE_DISCOVERY","STRATEGY_DEPENDENCY","HUMAN_EMERGENCY_CONTROLS"]
def audit(*, offsite_dr=False, live_factor_history=False, live_queue_telemetry=False, live_post_fill_telemetry=False, fee_fx_complete=False):
    status={x:"FULL" for x in TOPICS}; reasons={}
    if not offsite_dr: status["DISASTER_RECOVERY"]="PARTIAL"; reasons["DISASTER_RECOVERY"]="OFFSITE_REPLICATION_REQUIRED"
    if not live_factor_history: status["DYNAMIC_PORTFOLIO_CORRELATION"]="PARTIAL"; reasons["DYNAMIC_PORTFOLIO_CORRELATION"]="LIVE_FACTOR_HISTORY_REQUIRED"
    if not live_queue_telemetry: status["QUEUE_POSITION"]="PARTIAL"; reasons["QUEUE_POSITION"]="LIVE_QUEUE_TELEMETRY_REQUIRED"
    if not live_post_fill_telemetry: status["FILL_TOXICITY"]="PARTIAL"; reasons["FILL_TOXICITY"]="POST_FILL_TELEMETRY_REQUIRED"
    if not fee_fx_complete: status["EXACT_FILL_PNL_ACCOUNTING"]="PARTIAL"; reasons["EXACT_FILL_PNL_ACCOUNTING"]="COMMISSION_ASSET_FX_REQUIRED"
    return {"topics":status,"reasons":reasons,"full":sum(v=="FULL" for v in status.values()),"partial":sum(v!="FULL" for v in status.values()),"architecture_complete":all(v=="FULL" for v in status.values()),"live_authorized":False}
