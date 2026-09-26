import unittest
from datetime import datetime, timezone, timedelta

from ready_bot.trade_management import (
    adaptive_trade_levels,
    build_entry_zone,
    build_invalidation,
    classify_trade_setup,
    entry_in_zone,
    liquidity_quote_cap,
    momentum_failure,
    net_reward_risk,
    profit_protection_state,
    reentry_status,
    register_exit_for_reentry,
    setup_excursion_profile,
    thesis_invalidated,
    trade_quality_degradation_status,
)


CFG={
    "momentum_ignition_min_score":90,
    "momentum_ignition_min_rvol":1.8,
    "momentum_ignition_min_velocity":.0008,
    "pullback_max_vwap_atr":.6,
    "pullback_min_score":85,
    "failed_breakout_tolerance_pct":.0015,
    "failure_taker_below":.50,
    "failure_obi_below":.48,
    "failure_rvol_below":.80,
    "momentum_failure_min_signals":3,
    "continuation_taker_min":.56,
    "continuation_obi_min":.55,
    "continuation_rvol_min":1.0,
    "reduce_risk_at_r":.5,
    "reduced_risk_remaining_r":.5,
    "breakeven_at_r":1.0,
    "lock_profit_at_r":1.5,
    "lock_profit_r":.5,
    "trail_from_r":2.0,
    "trailing_atr_multiplier":1.5,
    "swing_trail_atr_buffer":.15,
    "hard_mae_failure_r":.95,
    "min_adaptive_mae_failure_r":.55,
    "mae_buffer_r":.15,
    "maximum_preexecution_score_drop":10,
    "minimum_preexecution_flow_score":35,
    "reentry_cooldown_minutes":5,
    "max_reentry_attempts_per_window":2,
    "reentry_attempt_window_minutes":60,
    "reentry_min_score":90,
    "setup_rules":{
        "MOMENTUM_IGNITION":{"enabled":True,"entry_zone_below_atr":.1,"entry_zone_above_atr":.25,"retest_tolerance_pct":.0015,"invalidation_atr_buffer":.15,"no_follow_through_candles":5},
        "BREAKOUT_RETEST":{"enabled":True,"entry_zone_below_atr":.2,"entry_zone_above_atr":.3,"retest_tolerance_pct":.0015,"invalidation_atr_buffer":.15,"no_follow_through_candles":8},
        "PULLBACK_CONTINUATION":{"enabled":True,"entry_zone_below_atr":.2,"entry_zone_above_atr":.35,"retest_tolerance_pct":.002,"invalidation_atr_buffer":.2,"no_follow_through_candles":10},
    },
    "paper_partial_profit":{"tp1_r":1.0,"tp2_r":2.0,"tp1_r_min":.75,"tp1_r_max":1.5,"tp2_r_min":1.5,"tp2_r_max":3.0},
}


def snap(score=92,rvol=2.0,velocity=.001,resistance=100,vwap=99.8):
    return {
        "ask":100.05,"bid":100.0,
        "micro_pre":{
            "atr_1m":.5,"vwap":vwap,"vwap_distance_atr":.4,
            "ema9_slope_positive":True,"rvol_1m":rvol,"price_velocity":velocity,
            "taker_rising":True,"taker_latest":.60,"cvd_positive":True,
            "micro_breakout_hold":{"ok":True,"resistance":resistance,"breakout":True,"hold":True},
            "breakout":{"resistance":resistance,"distance_atr":.1},
        },
        "micro":{
            "score":score,"rvol_1m":rvol,"price_velocity":velocity,"taker_rising":True,
            "obi":.62,
            "agg_cvd":{"ratio":.61,"delta_quote":1000,"slope_positive":True},
            "micro_breakout_hold":{"ok":True,"resistance":resistance,"breakout":True,"hold":True},
        },
    }


class TradeManagementTests(unittest.TestCase):
    def test_classifier_distinguishes_ignition_from_retest(self):
        self.assertEqual(classify_trade_setup(snap(),CFG),"MOMENTUM_IGNITION")
        x=snap(score=87,rvol=1.5,velocity=.0002)
        self.assertEqual(classify_trade_setup(x,CFG),"BREAKOUT_RETEST")

    def test_entry_zone_rejects_chased_fill(self):
        x=snap(score=87,rvol=1.5,velocity=.0002)
        setup=classify_trade_setup(x,CFG)
        zone=build_entry_zone(x,setup,CFG)
        self.assertTrue(zone["ok"])
        self.assertTrue(entry_in_zone(100.05,zone))
        self.assertFalse(entry_in_zone(101.0,zone))

    def test_invalidation_is_tighter_than_catastrophe_stop(self):
        x=snap(score=87,rvol=1.5,velocity=.0002)
        inv=build_invalidation(x,"BREAKOUT_RETEST",100.05,98.0,CFG)
        self.assertTrue(inv["ok"])
        self.assertGreaterEqual(inv["level"],98.0)
        self.assertLess(inv["level"],100.05)

    def test_liquidity_position_cap_uses_only_nearby_depth(self):
        d={"asks":[["100","1"],["100.05","1"],["101","10"]]}
        cap=liquidity_quote_cap(d,10,.25)
        self.assertGreater(cap,0)
        self.assertLess(cap,100)

    def test_net_rr_includes_fees_and_exit_slippage(self):
        r=net_reward_risk(100,99,102,.001,3,8)
        self.assertIsNotNone(r["net_rr"])
        self.assertLess(r["net_rr"],2.0)
        self.assertGreater(r["net_rr"],1.0)

    def test_mae_mfe_adapts_only_after_sample(self):
        small=[{"setup_type":"BREAKOUT_RETEST","pnl_usdt":.1,"mfe_r":1.5,"mae_r":.3} for _ in range(10)]
        p=setup_excursion_profile(small,"BREAKOUT_RETEST",30)
        self.assertFalse(p["ready"])
        a=adaptive_trade_levels(p,CFG)
        self.assertFalse(a["adaptive"])

        rows=[{"setup_type":"BREAKOUT_RETEST","pnl_usdt":.1,"mfe_r":1.8+(i%5)*.1,"mae_r":.25+(i%4)*.05} for i in range(40)]
        p=setup_excursion_profile(rows,"BREAKOUT_RETEST",30)
        self.assertTrue(p["ready"])
        a=adaptive_trade_levels(p,CFG)
        self.assertTrue(a["adaptive"])
        self.assertLessEqual(a["mae_failure_r"],CFG["hard_mae_failure_r"])

    def test_failed_breakout_requires_price_and_flow_failure(self):
        p={"setup_type":"BREAKOUT_RETEST","stop":98,"invalidation":{"level":99.6,"resistance":100}}
        x=snap(score=80,rvol=.6,velocity=0)
        x["bid"]=99.7
        x["micro"]["obi"]=.40
        x["micro"]["agg_cvd"]={"ratio":.45,"delta_quote":-100,"slope_positive":False}
        x["micro_pre"]["cvd_positive"]=False
        x["micro_pre"]["taker_latest"]=.45
        out=thesis_invalidated(p,x,CFG)
        self.assertTrue(out["invalid"])
        self.assertEqual(out["reason"],"FAILED_BREAKOUT")

    def test_momentum_failure_requires_multiple_independent_failures(self):
        p={}
        x=snap()
        x["micro"]["obi"]=.40
        x["micro"]["agg_cvd"]={"ratio":.45,"delta_quote":-100,"slope_positive":False}
        x["micro"]["rvol_1m"]=.5
        x["micro_pre"]["cvd_positive"]=False
        out=momentum_failure(p,x,CFG)
        self.assertTrue(out["failed"])
        self.assertGreaterEqual(out["flow"]["failure_count"],3)

    def test_profit_ladder_reduces_then_locks_and_trails(self):
        p={"entry":100,"stop":98,"initial_risk_abs":2,"profit_stage":"INITIAL"}
        flow={"continuation_strong":True}
        a=profit_protection_state(p,101.2,.5,100.2,flow,CFG)
        self.assertGreater(a["stop"],98)
        b=profit_protection_state(p,104.2,.5,103.0,flow,CFG)
        self.assertGreaterEqual(b["stop"],101)
        self.assertEqual(b["stage"],"TRAILING")

    def test_reentry_has_cooldown_and_attempt_cap(self):
        state={"reentry":{}}
        now=datetime.now(timezone.utc)
        register_exit_for_reentry(state,"SOLUSDT","FAILED_BREAKOUT","BREAKOUT_RETEST",CFG,now)
        x=reentry_status(state,"SOLUSDT",snap(),CFG,now+timedelta(minutes=1))
        self.assertFalse(x["ok"])
        x=reentry_status(state,"SOLUSDT",snap(),CFG,now+timedelta(minutes=6))
        self.assertTrue(x["ok"])
        register_exit_for_reentry(state,"SOLUSDT","STOP","BREAKOUT_RETEST",CFG,now+timedelta(minutes=7))
        x=reentry_status(state,"SOLUSDT",snap(),CFG,now+timedelta(minutes=13))
        self.assertFalse(x["ok"])
        self.assertEqual(x["reason"],"REENTRY_ATTEMPTS_EXHAUSTED")

    def test_quality_degradation_cancels_late_trade(self):
        original=snap()["micro"]
        current={"obi":.40,"taker_ratio":.48,"cvd_delta":-100,"cvd_slope_positive":False}
        x=trade_quality_degradation_status(original,current,CFG)
        self.assertFalse(x["ok"])
        self.assertEqual(x["reason"],"TRADE_QUALITY_DEGRADED")


if __name__=="__main__":
    unittest.main()
