"""Deep readiness intelligence: calibrated evidence, abstention, transitions, portfolio factors and capacity.

Pure deterministic primitives. They never authorize live trading or increase risk.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
import math, statistics

def _f(v,d=0.0):
    try:
        x=float(v); return x if math.isfinite(x) else d
    except Exception:return d

def calibration_table(rows, *, score_bins=((70,75),(75,80),(80,85),(85,90),(90,95),(95,101)), min_sample=30):
    groups=defaultdict(list)
    for r in rows or []:
        score=_f(r.get("score"),-1); setup=str(r.get("setup_type") or "UNKNOWN"); regime=str(r.get("regime") or "UNKNOWN")
        liq=str(r.get("liquidity_bucket") or "UNKNOWN"); vol=str(r.get("volatility_bucket") or "UNKNOWN")
        for lo,hi in score_bins:
            if lo<=score<hi:
                groups[(lo,hi,setup,regime,liq,vol)].append(r); break
    out={}
    for k,a in groups.items():
        n=len(a); wins=sum(_f(x.get("net_pnl_usdt"))>0 for x in a)
        pnl=sum(_f(x.get("net_pnl_usdt")) for x in a)
        key=f"{k[0]}-{k[1]}|{k[2]}|{k[3]}|{k[4]}|{k[5]}"
        out[key]={"n":n,"net_win_probability":wins/n if n else None,"mean_net_pnl_usdt":pnl/n if n else None,
                  "evidence_ready":n>=int(min_sample)}
    return {"groups":out,"min_sample":int(min_sample),"diagnostic_only":True,"may_authorize_live":False}

def calibrated_decision(features, calibration, *, min_probability=.55, min_ev_usdt=0.0):
    key=str(features.get("calibration_key") or "")
    g=(calibration or {}).get("groups",{}).get(key)
    uncertainty=[]
    if not g or not g.get("evidence_ready"): uncertainty.append("CALIBRATION_INSUFFICIENT")
    for name in ("data_confidence","liquidity_confidence","regime_confidence"):
        if _f(features.get(name),0)<.7: uncertainty.append(name.upper()+"_LOW")
    if str(features.get("regime_state") or "") in {"REGIME_TRANSITION","REGIME_UNCERTAIN"}: uncertainty.append("REGIME_NOT_STABLE")
    if uncertainty:
        return {"decision":"UNKNOWN","abstain":True,"reasons":sorted(set(uncertainty)),"may_authorize_live":False}
    p=_f(g.get("net_win_probability")); ev=_f(g.get("mean_net_pnl_usdt"))
    ok=p>=float(min_probability) and ev>float(min_ev_usdt)
    return {"decision":"BUY" if ok else "SKIP","abstain":not ok,"calibrated_probability":p,"calibrated_ev_usdt":ev,"may_authorize_live":False}

def regime_transition(prev, cur, *, vol_jump=1.5, corr_jump=.20, btc_move_jump=.015):
    reasons=[]
    if prev and cur:
        if _f(cur.get("realized_vol"))>max(1e-12,_f(prev.get("realized_vol")))*vol_jump: reasons.append("VOL_EXPANSION")
        if abs(_f(cur.get("median_correlation"))-_f(prev.get("median_correlation")))>=corr_jump: reasons.append("CORRELATION_SHIFT")
        if abs(_f(cur.get("btc_return"))-_f(prev.get("btc_return")))>=btc_move_jump: reasons.append("BTC_STATE_SHIFT")
        if cur.get("label") and prev.get("label") and cur.get("label")!=prev.get("label"): reasons.append("CLASSIFIER_LABEL_CHANGE")
    confidence=_f((cur or {}).get("confidence"),0)
    state="REGIME_UNCERTAIN" if confidence<.65 else "REGIME_TRANSITION" if reasons else "REGIME_STABLE"
    return {"state":state,"reasons":reasons,"size_multiplier":Decimal("0") if state=="REGIME_UNCERTAIN" else Decimal(".5") if state=="REGIME_TRANSITION" else Decimal("1")}

def _corr(a,b):
    n=min(len(a),len(b))
    if n<5:return None
    x=list(map(float,a[-n:])); y=list(map(float,b[-n:])); mx=sum(x)/n; my=sum(y)/n
    sx=sum((v-mx)**2 for v in x); sy=sum((v-my)**2 for v in y)
    if sx<=0 or sy<=0:return None
    return sum((x[i]-mx)*(y[i]-my) for i in range(n))/math.sqrt(sx*sy)

def dynamic_portfolio_exposure(positions, returns_by_symbol, btc_returns=None, eth_returns=None):
    rows=[]; total=sum(max(0,_f(p.get("risk_usdt"))) for p in positions or [])
    concentration=0.0
    for p in positions or []:
        sym=str(p.get("symbol")); rs=returns_by_symbol.get(sym,[])
        cb=_corr(rs,btc_returns or []); ce=_corr(rs,eth_returns or [])
        peers=[]
        for q in positions or []:
            if q is p: continue
            c=_corr(rs,returns_by_symbol.get(str(q.get("symbol")),[]))
            if c is not None: peers.append(c)
        avg=max(peers) if peers else 0.0
        risk=max(0,_f(p.get("risk_usdt")))
        concentration+=risk*max(0,avg)
        rows.append({"symbol":sym,"risk_usdt":risk,"btc_corr":cb,"eth_corr":ce,"max_peer_corr":avg})
    effective=total+concentration
    return {"positions":rows,"nominal_risk_usdt":total,"correlation_addon_usdt":concentration,
            "effective_portfolio_risk_usdt":effective,"fail_closed":True}

def portfolio_stress(positions, scenarios):
    out={}
    for name,shock in (scenarios or {}).items():
        loss=0.0
        for p in positions or []:
            beta=_f(p.get("btc_beta"),1); notional=max(0,_f(p.get("notional_usdt")))
            spread_mult=max(1,_f(shock.get("spread_mult"),1)); slip_mult=max(1,_f(shock.get("slippage_mult"),1))
            move=_f(shock.get("btc_move"))*beta
            liq=_f(shock.get("alt_liquidity_change"))
            loss+=max(0,-move)*notional + max(0,-liq)*notional*.05 + (spread_mult-1)*notional*.0002 + (slip_mult-1)*notional*.0003
        out[str(name)]={"projected_loss_usdt":loss}
    return {"scenarios":out,"worst_projected_loss_usdt":max([x["projected_loss_usdt"] for x in out.values()] or [0]),"diagnostic_only":True}

def safe_capacity(*, order_usdt, bid_ask_depth_usdt, recent_flow_usdt, spread_bps, volatility, max_depth_fraction=.05, max_flow_fraction=.02):
    depth=max(0,_f(bid_ask_depth_usdt)); flow=max(0,_f(recent_flow_usdt))
    cap=min(depth*max_depth_fraction,flow*max_flow_fraction)
    penalty=max(0.1,1.0-min(.8,_f(spread_bps)/100.0)-min(.8,_f(volatility)*2))
    safe=cap*penalty
    requested=max(0,_f(order_usdt))
    return {"safe_capacity_usdt":safe,"requested_usdt":requested,"within_capacity":requested<=safe and safe>0,
            "utilization":requested/safe if safe>0 else None,"fail_closed":safe<=0 or requested>safe}
