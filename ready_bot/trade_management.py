"""Trade-level intelligence for the V3 Binance Spot PAPER engine.

This module manages the thesis of a trade, not signal discovery. It is designed
to be deterministic, setup-aware, and fail-closed. Adaptive MAE/MFE behaviour
only activates after an explicit minimum sample.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone, timedelta


SUPPORTED_SETUPS = {
    "MOMENTUM_IGNITION",
    "BREAKOUT_RETEST",
    "PULLBACK_CONTINUATION",
}


def _finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def percentile(values, q):
    xs=sorted(float(x) for x in values if _finite(x))
    if not xs:
        return None
    if len(xs)==1:
        return xs[0]
    pos=(len(xs)-1)*float(q)
    lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi:
        return xs[lo]
    w=pos-lo
    return xs[lo]*(1-w)+xs[hi]*w


def classify_trade_setup(snap, cfg):
    micro=snap.get("micro") or {}
    pre=snap.get("micro_pre") or {}
    hold=micro.get("micro_breakout_hold") or pre.get("micro_breakout_hold") or {}
    rvol=float(micro.get("rvol_1m") or pre.get("rvol_1m") or 0.0)
    velocity=float(micro.get("price_velocity") or pre.get("price_velocity") or 0.0)
    vwap_dist=pre.get("vwap_distance_atr")
    score=float(micro.get("score") or 0.0)

    # A genuine ignition is acceleration-led, even if a tiny micro-breakout/hold
    # also occurred. This keeps it distinct from a slower structural retest.
    if (
        score>=float(cfg["momentum_ignition_min_score"])
        and rvol>=float(cfg["momentum_ignition_min_rvol"])
        and bool(micro.get("taker_rising") or pre.get("taker_rising"))
        and velocity>=float(cfg["momentum_ignition_min_velocity"])
    ):
        return "MOMENTUM_IGNITION"

    if hold.get("ok") is True:
        return "BREAKOUT_RETEST"

    if (
        vwap_dist is not None
        and 0.0<=float(vwap_dist)<=float(cfg["pullback_max_vwap_atr"])
        and bool(pre.get("ema9_slope_positive"))
        and score>=float(cfg["pullback_min_score"])
    ):
        return "PULLBACK_CONTINUATION"

    return "UNSUPPORTED_SETUP"


def setup_rules(setup, cfg):
    rules=(cfg.get("setup_rules") or {}).get(setup)
    if not isinstance(rules,dict) or rules.get("enabled") is not True:
        return None
    return rules


def build_entry_zone(snap, setup, cfg):
    pre=snap.get("micro_pre") or {}
    micro=snap.get("micro") or {}
    ask=float(snap["ask"])
    atr=float(pre.get("atr_1m") or 0.0)
    vwap=pre.get("vwap")
    hold=micro.get("micro_breakout_hold") or pre.get("micro_breakout_hold") or {}
    resistance=hold.get("resistance") or (pre.get("breakout") or {}).get("resistance")
    rules=setup_rules(setup,cfg)
    if rules is None or atr<=0:
        return {"ok":False,"reason":"ENTRY_ZONE_INPUT_MISSING"}

    low=ask-float(rules["entry_zone_below_atr"])*atr
    high=ask+float(rules["entry_zone_above_atr"])*atr

    if setup=="BREAKOUT_RETEST" and resistance and _finite(resistance):
        r=float(resistance)
        low=max(low,r*(1-float(rules["retest_tolerance_pct"])))
        high=min(high,r+float(rules["entry_zone_above_atr"])*atr)
    elif setup=="PULLBACK_CONTINUATION" and vwap and _finite(vwap):
        vw=float(vwap)
        low=max(low,vw-float(rules["entry_zone_below_atr"])*atr)
        high=min(high,vw+float(rules["entry_zone_above_atr"])*atr)
    elif setup=="MOMENTUM_IGNITION":
        if vwap and _finite(vwap):
            low=max(low,float(vwap))
        if resistance and _finite(resistance):
            low=max(low,float(resistance)*(1-float(rules["retest_tolerance_pct"])))

    if not (_finite(low) and _finite(high)) or low<=0 or high<low:
        return {"ok":False,"reason":"ENTRY_ZONE_INVALID"}
    return {
        "ok":True,
        "low":low,
        "high":high,
        "reference_ask":ask,
        "atr_1m":atr,
        "resistance":float(resistance) if resistance and _finite(resistance) else None,
        "vwap":float(vwap) if vwap and _finite(vwap) else None,
    }


def entry_in_zone(fill_price, zone):
    return bool(zone and zone.get("ok") and float(zone["low"])<=float(fill_price)<=float(zone["high"]))


def build_invalidation(snap, setup, entry, stop, cfg):
    pre=snap.get("micro_pre") or {}
    micro=snap.get("micro") or {}
    rules=setup_rules(setup,cfg)
    if rules is None:
        return {"ok":False,"reason":"UNSUPPORTED_SETUP"}
    atr=float(pre.get("atr_1m") or 0.0)
    hold=micro.get("micro_breakout_hold") or pre.get("micro_breakout_hold") or {}
    resistance=hold.get("resistance") or (pre.get("breakout") or {}).get("resistance")
    vwap=pre.get("vwap")

    if setup=="BREAKOUT_RETEST" and resistance and atr>0:
        level=float(resistance)-float(rules["invalidation_atr_buffer"])*atr
    elif setup=="MOMENTUM_IGNITION" and vwap and atr>0:
        structural=float(resistance) if resistance and _finite(resistance) else float(vwap)
        level=max(float(vwap)-float(rules["invalidation_atr_buffer"])*atr,
                  structural-float(rules["invalidation_atr_buffer"])*atr)
    elif setup=="PULLBACK_CONTINUATION" and vwap and atr>0:
        level=float(vwap)-float(rules["invalidation_atr_buffer"])*atr
    else:
        level=float(stop)

    # Thesis invalidation is allowed to be tighter than the catastrophe stop,
    # but never below the protective stop.
    level=max(float(stop),min(float(entry)*(1-1e-6),float(level)))
    return {
        "ok":True,
        "level":level,
        "resistance":float(resistance) if resistance and _finite(resistance) else None,
        "vwap":float(vwap) if vwap and _finite(vwap) else None,
        "requires_flow_confirmation":True,
    }


def liquidity_quote_cap(depth, max_slippage_bps, utilization_fraction=0.25):
    asks=(depth or {}).get("asks") or []
    if not asks:
        return 0.0
    best=float(asks[0][0])
    if best<=0:
        return 0.0
    max_px=best*(1+float(max_slippage_bps)/10000.0)
    quote=0.0
    for px0,qty0 in asks:
        px=float(px0); qty=float(qty0)
        if px>max_px:
            break
        quote+=px*qty
    return max(0.0,quote*float(utilization_fraction))


def net_reward_risk(entry, stop, target, fee_rate, entry_slip_bps, exit_slip_bps):
    e=float(entry); s=float(stop); t=float(target); f=float(fee_rate)
    es=max(0.0,float(entry_slip_bps))/10000.0
    xs=max(0.0,float(exit_slip_bps))/10000.0
    # Entry price supplied is already the modeled fill, so entry slippage is
    # informational. Risk/reward includes fees and conservative exit slippage.
    entry_cash=e*(1+f)
    stop_cash=s*(1-f)*(1-xs)
    target_cash=t*(1-f)*(1-xs)
    risk=entry_cash-stop_cash
    reward=target_cash-entry_cash
    rr=reward/risk if risk>0 else None
    return {
        "net_rr":rr,
        "net_reward_per_unit":reward,
        "net_risk_per_unit":risk,
        "entry_slippage_bps":float(entry_slip_bps),
        "assumed_exit_slippage_bps":float(exit_slip_bps),
    }



def execution_flow_score(*, obi, taker_ratio, cvd_delta, cvd_slope_positive):
    score=0.0
    if obi is not None:
        if float(obi)>=0.58:
            score+=15
        elif float(obi)>=0.55:
            score+=8
    if taker_ratio is not None:
        if float(taker_ratio)>=0.57:
            score+=20
        elif float(taker_ratio)>=0.53:
            score+=10
    if float(cvd_delta or 0)>0 and bool(cvd_slope_positive):
        score+=15
    return score


def trade_quality_degradation_status(original_micro, current_flow, cfg):
    agg=(original_micro or {}).get("agg_cvd") or {}
    original=execution_flow_score(
        obi=(original_micro or {}).get("obi"),
        taker_ratio=agg.get("ratio"),
        cvd_delta=agg.get("delta_quote"),
        cvd_slope_positive=agg.get("slope_positive"),
    )
    current=execution_flow_score(
        obi=current_flow.get("obi"),
        taker_ratio=current_flow.get("taker_ratio"),
        cvd_delta=current_flow.get("cvd_delta"),
        cvd_slope_positive=current_flow.get("cvd_slope_positive"),
    )
    drop=original-current
    degraded=bool(
        current<float(cfg["minimum_preexecution_flow_score"])
        or drop>float(cfg["maximum_preexecution_score_drop"])
    )
    return {
        "ok":not degraded,
        "original_flow_score":original,
        "current_flow_score":current,
        "score_drop":drop,
        "reason":"TRADE_QUALITY_DEGRADED" if degraded else None,
    }

def setup_excursion_profile(closed_trades, setup, min_sample=30):
    rows=[
        x for x in (closed_trades or [])
        if x.get("setup_type")==setup
        and _finite(x.get("mfe_r"))
        and _finite(x.get("mae_r"))
    ]
    winners=[x for x in rows if float(x.get("pnl_usdt") or 0)>0]
    if len(rows)<int(min_sample) or len(winners)<max(10,int(min_sample)//3):
        return {
            "ready":False,
            "sample":len(rows),
            "winner_sample":len(winners),
        }
    winner_mae=[float(x["mae_r"]) for x in winners]
    winner_mfe=[float(x["mfe_r"]) for x in winners]
    return {
        "ready":True,
        "sample":len(rows),
        "winner_sample":len(winners),
        "winner_mae_p90":percentile(winner_mae,.90),
        "winner_mfe_p50":percentile(winner_mfe,.50),
        "winner_mfe_p75":percentile(winner_mfe,.75),
        "winner_mfe_p90":percentile(winner_mfe,.90),
    }


def adaptive_trade_levels(profile, cfg):
    defaults=cfg["paper_partial_profit"]
    out={
        "adaptive":False,
        "mae_failure_r":float(cfg["hard_mae_failure_r"]),
        "tp1_r":float(defaults["tp1_r"]),
        "tp2_r":float(defaults["tp2_r"]),
    }
    if not profile or profile.get("ready") is not True:
        return out
    mae=profile.get("winner_mae_p90")
    p50=profile.get("winner_mfe_p50")
    p75=profile.get("winner_mfe_p75")
    if mae is not None:
        out["mae_failure_r"]=min(
            float(cfg["hard_mae_failure_r"]),
            max(float(cfg["min_adaptive_mae_failure_r"]),float(mae)+float(cfg["mae_buffer_r"]))
        )
    if p50 is not None:
        out["tp1_r"]=min(float(defaults["tp1_r_max"]),max(float(defaults["tp1_r_min"]),float(p50)))
    if p75 is not None:
        out["tp2_r"]=min(float(defaults["tp2_r_max"]),max(out["tp1_r"]+0.25,float(p75)))
    out["adaptive"]=True
    return out


def current_flow_state(snap, cfg):
    pre=snap.get("micro_pre") or {}
    micro=snap.get("micro") or {}
    agg=micro.get("agg_cvd") or {}
    taker=agg.get("ratio")
    if taker is None:
        taker=pre.get("taker_latest")
    obi=micro.get("obi")
    rvol=micro.get("rvol_1m")
    if rvol is None:
        rvol=pre.get("rvol_1m")
    cvd_positive=bool(
        (float(agg.get("delta_quote") or 0)>0 and agg.get("slope_positive") is True)
        or pre.get("cvd_positive") is True
    )
    failures=[]
    if taker is not None and float(taker)<float(cfg["failure_taker_below"]):
        failures.append("TAKER_WEAK")
    if not cvd_positive:
        failures.append("CVD_NEGATIVE")
    if obi is not None and float(obi)<float(cfg["failure_obi_below"]):
        failures.append("OBI_BEARISH")
    if rvol is not None and float(rvol)<float(cfg["failure_rvol_below"]):
        failures.append("RVOL_COLLAPSED")
    return {
        "taker":taker,
        "obi":obi,
        "rvol_1m":rvol,
        "cvd_positive":cvd_positive,
        "failures":failures,
        "failure_count":len(failures),
        "continuation_strong":bool(
            taker is not None and float(taker)>=float(cfg["continuation_taker_min"])
            and cvd_positive
            and (obi is None or float(obi)>=float(cfg["continuation_obi_min"]))
            and (rvol is None or float(rvol)>=float(cfg["continuation_rvol_min"]))
        ),
    }


def thesis_invalidated(position, snap, cfg):
    level=float((position.get("invalidation") or {}).get("level") or position.get("stop") or 0)
    bid=float(snap.get("bid") or 0)
    flow=current_flow_state(snap,cfg)
    setup=position.get("setup_type")
    resistance=(position.get("invalidation") or {}).get("resistance")
    back_inside=bool(resistance and bid<float(resistance)*(1-float(cfg["failed_breakout_tolerance_pct"])))
    price_invalid=bid<=level if level>0 else False

    if setup=="BREAKOUT_RETEST" and back_inside and flow["failure_count"]>=2:
        return {"invalid":True,"reason":"FAILED_BREAKOUT","flow":flow,"price_invalid":price_invalid}
    if price_invalid and flow["failure_count"]>=2:
        return {"invalid":True,"reason":"THESIS_INVALIDATED","flow":flow,"price_invalid":True}
    return {"invalid":False,"reason":None,"flow":flow,"price_invalid":price_invalid}


def momentum_failure(position, snap, cfg):
    flow=current_flow_state(snap,cfg)
    return {
        "failed":flow["failure_count"]>=int(cfg["momentum_failure_min_signals"]),
        "flow":flow,
    }


def profit_protection_state(position, bid, atr_now, swing_low, flow, cfg):
    r0=max(float(position.get("initial_risk_abs") or 0),1e-12)
    r=(float(bid)-float(position["entry"]))/r0
    current=float(position["stop"])
    stage=str(position.get("profit_stage") or "INITIAL")
    proposed=current

    if r>=float(cfg["reduce_risk_at_r"]):
        proposed=max(proposed,float(position["entry"])-r0*float(cfg["reduced_risk_remaining_r"]))
        stage="RISK_REDUCED"
    if r>=float(cfg["breakeven_at_r"]) and flow.get("continuation_strong"):
        proposed=max(proposed,float(position["entry"]))
        stage="BREAKEVEN"
    if r>=float(cfg["lock_profit_at_r"]):
        proposed=max(proposed,float(position["entry"])+r0*float(cfg["lock_profit_r"]))
        stage="PROFIT_LOCKED"
    if r>=float(cfg["trail_from_r"]):
        trails=[]
        if atr_now and float(atr_now)>0:
            trails.append(float(bid)-float(cfg["trailing_atr_multiplier"])*float(atr_now))
        if swing_low and _finite(swing_low):
            trails.append(float(swing_low)-float(cfg["swing_trail_atr_buffer"])*float(atr_now or 0))
        if trails:
            proposed=max(proposed,max(trails))
            stage="TRAILING"

    return {"stage":stage,"stop":min(float(bid)*(1-1e-6),proposed),"r":r}


def reentry_status(state, symbol, snap, cfg, now=None):
    row=(state.get("reentry") or {}).get(symbol) or {}
    current=now or datetime.now(timezone.utc)
    until=row.get("blocked_until")
    if until:
        try:
            dt=datetime.fromisoformat(str(until).replace("Z","+00:00"))
            if current<dt:
                return {"ok":False,"reason":"REENTRY_COOLDOWN","blocked_until":until}
        except Exception:
            return {"ok":False,"reason":"REENTRY_STATE_INVALID"}

    attempts=int(row.get("attempts") or 0)
    if attempts>=int(cfg["max_reentry_attempts_per_window"]):
        ws=row.get("window_started_at")
        try:
            start=datetime.fromisoformat(str(ws).replace("Z","+00:00"))
        except Exception:
            start=current
        if current-start<timedelta(minutes=float(cfg["reentry_attempt_window_minutes"])):
            return {"ok":False,"reason":"REENTRY_ATTEMPTS_EXHAUSTED","attempts":attempts}

    # Re-entry must be a rebuilt setup, not the same failed thesis.
    micro=snap.get("micro") or {}
    score=float(micro.get("score") or 0.0)
    if attempts>0 and score<float(cfg["reentry_min_score"]):
        return {"ok":False,"reason":"REENTRY_SETUP_NOT_REBUILT","score":score}
    if attempts>0 and not bool((micro.get("micro_breakout_hold") or {}).get("ok")):
        return {"ok":False,"reason":"REENTRY_NO_NEW_HOLD"}
    return {"ok":True,"reason":None,"attempts":attempts}


def register_exit_for_reentry(state, symbol, reason, setup_type, cfg, now=None):
    current=now or datetime.now(timezone.utc)
    re=state.setdefault("reentry",{})
    row=dict(re.get(symbol) or {})
    loss_like=reason in {
        "STOP","FAILED_BREAKOUT","THESIS_INVALIDATED","MOMENTUM_FAILURE",
        "TIME_NO_FOLLOW_THROUGH","MAE_FAILURE"
    }
    if not loss_like:
        re.pop(symbol,None)
        return None

    window_minutes=float(cfg["reentry_attempt_window_minutes"])
    try:
        started=datetime.fromisoformat(str(row.get("window_started_at")).replace("Z","+00:00"))
    except Exception:
        started=current
    if current-started>=timedelta(minutes=window_minutes):
        attempts=0
        started=current
    else:
        attempts=int(row.get("attempts") or 0)
    attempts+=1
    cooldown=int(cfg["reentry_cooldown_minutes"])
    blocked=current+timedelta(minutes=cooldown)
    re[symbol]={
        "attempts":attempts,
        "window_started_at":started.isoformat(),
        "blocked_until":blocked.isoformat(),
        "last_reason":reason,
        "last_setup_type":setup_type,
    }
    return re[symbol]


def attribute_trade(position, pnl_usdt, exit_reason):
    ctx=position.get("entry_context") or {}
    rs=position.get("relative_strength") or {}
    good=[]
    bad=[]
    if float((rs or {}).get("score") or 0)>=65:
        good.append("STRONG_RELATIVE_STRENGTH")
    if float(ctx.get("rvol_1m") or 0)>=1.8:
        good.append("VOLUME_EXPANSION")
    agg=ctx.get("agg_cvd") or {}
    if float(agg.get("delta_quote") or 0)>0:
        good.append("POSITIVE_CVD")
    if float(ctx.get("obi") or 0)>=0.58:
        good.append("BULLISH_OBI")
    if float(ctx.get("estimated_entry_slippage_bps") or 999)<=4:
        good.append("LOW_ENTRY_SLIPPAGE")

    if float(ctx.get("vwap_distance_atr") or 0)>1.0:
        bad.append("LATE_VWAP_EXTENSION")
    if float(ctx.get("decision_latency_ms") or 0)>4000:
        bad.append("HIGH_DECISION_LATENCY")
    if float(ctx.get("estimated_entry_slippage_bps") or 0)>8:
        bad.append("HIGH_ENTRY_SLIPPAGE")
    if exit_reason in {"FAILED_BREAKOUT","THESIS_INVALIDATED"}:
        bad.append("STRUCTURE_FAILURE")
    if exit_reason=="MOMENTUM_FAILURE":
        bad.append("FLOW_FAILURE")
    if exit_reason=="TIME_NO_FOLLOW_THROUGH":
        bad.append("NO_FOLLOW_THROUGH")
    if exit_reason=="STOP":
        bad.append("STOP_REACHED")

    return {
        "outcome":"WIN" if float(pnl_usdt)>0 else ("LOSS" if float(pnl_usdt)<0 else "FLAT"),
        "positive_factors":good,
        "negative_factors":bad,
        "setup_type":position.get("setup_type"),
        "exit_reason":exit_reason,
    }
