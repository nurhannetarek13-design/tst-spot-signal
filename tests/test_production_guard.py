import unittest
from datetime import datetime, timezone

from ready_bot.production_guard import (
    adverse_selection_status,
    btc_shock_status,
    clock_sync_status,
    execution_quality_status,
    estimate_sell_slippage,
    liquidity_disappearance_status,
    listing_age_status,
    signal_freshness_status,
    warmup_status,
)
from ready_bot.smart_execution import choose_execution_plan, update_symbol_slippage_model


class ProductionGuardTests(unittest.TestCase):
    def test_clock_sync_budget(self):
        ok=clock_sync_status(10_500,10_000,11_000,max_offset_ms=100,max_rtt_ms=1500)
        self.assertTrue(ok["ok"])
        bad=clock_sync_status(12_000,10_000,11_000,max_offset_ms=100,max_rtt_ms=1500)
        self.assertFalse(bad["ok"])
        self.assertIn("CLOCK_OFFSET",bad["reasons"])

    def test_freshness_and_latency_are_separate(self):
        x=signal_freshness_status(
            detected_at_ms=1000,decision_at_ms=5000,now_ms=5200,market_event_ms=5000,
            max_signal_age_ms=500,max_decision_latency_ms=4500,
        )
        self.assertTrue(x["ok"])
        stale=signal_freshness_status(
            detected_at_ms=1000,decision_at_ms=5000,now_ms=8000,market_event_ms=5000,
            max_signal_age_ms=500,max_decision_latency_ms=4500,
        )
        self.assertIn("STALE_SIGNAL",stale["reasons"])

    def test_warmup_fails_closed(self):
        x=warmup_status(bars1m=20,bars3m=30,bars15m=220,bars1h=220,depth_samples=3)
        self.assertFalse(x["ok"])
        self.assertIn("1m",x["missing"])

    def test_btc_shock_detects_fast_move(self):
        bars=[]
        p=100
        for i in range(10):
            if i==9: p=98
            bars.append({"c":p})
        x=btc_shock_status(bars,shock_1m=.008,shock_3m=.015)
        self.assertTrue(x["shock"])

    def test_adverse_selection_rejects_bullish_book_negative_flow(self):
        micro={
            "obi":.65,
            "agg_cvd":{"delta_quote":-1000,"ratio":.45},
            "depth_flow":{"microprice_bias_bps":-1.0,"bid_liquidity_change_pct":-.40},
        }
        x=adverse_selection_status(micro)
        self.assertFalse(x["ok"])
        self.assertIn("OBI_CVD_DIVERGENCE",x["reasons"])
        self.assertIn("BID_LIQUIDITY_DISAPPEARANCE",x["reasons"])

    def test_liquidity_disappearance(self):
        x=liquidity_disappearance_status({"bid_liquidity_change_pct":-.5,"ask_liquidity_change_pct":.7})
        self.assertFalse(x["ok"])

    def test_depth_slippage_is_quote_size_aware(self):
        depth={"asks":[["100","0.05"],["101","1.0"]]}
        x=execution_quality_status(depth,10,max_slippage_bps=100,min_fill_ratio=.999)
        self.assertTrue(x["fill_ratio"]>=.999)
        self.assertGreater(x["slippage_bps"],0)

    def test_smart_execution_market_only_for_urgent_tight_book(self):
        cfg={
            "min_fill_ratio":.999,"max_slippage_bps":12,"max_latency_ms":6500,
            "market_momentum_score":92,"market_max_spread_bps":6,
            "market_max_slippage_bps":4,"aggressive_limit_max_cross_bps":8,
            "cancel_after_ms":1500,"max_cancel_replace":2,
        }
        market=choose_execution_plan(
            symbol="SOLUSDT",quote_amount_usdt=5,best_bid=100,best_ask=100.01,
            estimated_slippage_bps=2,fill_ratio=1,tick_size=.01,
            momentum_score=95,taker_rising=True,spread_bps=1,latency_ms=1000,cfg=cfg,
        )
        self.assertEqual(market["style"],"MARKET")
        limit=choose_execution_plan(
            symbol="SOLUSDT",quote_amount_usdt=5,best_bid=100,best_ask=100.01,
            estimated_slippage_bps=6,fill_ratio=1,tick_size=.01,
            momentum_score=88,taker_rising=True,spread_bps=4,latency_ms=1000,cfg=cfg,
        )
        self.assertEqual(limit["style"],"AGGRESSIVE_LIMIT")

    def test_per_symbol_slippage_model(self):
        model={}
        model=update_symbol_slippage_model(model,"SOLUSDT",4)
        model=update_symbol_slippage_model(model,"SOLUSDT",8)
        self.assertEqual(model["SOLUSDT"]["count"],2)
        self.assertGreater(model["SOLUSDT"]["ewma_bps"],4)


    def test_sell_depth_slippage_is_base_size_aware(self):
        depth={"bids":[["100","0.05"],["99","1.0"]]}
        x=estimate_sell_slippage(depth,0.10)
        self.assertGreaterEqual(x["fill_ratio"],0.999)
        self.assertLess(x["average_price"],100)
        self.assertGreater(x["slippage_bps"],0)


    def test_bid_cancellation_spike_is_rejected(self):
        x=liquidity_disappearance_status({
            "bid_liquidity_change_pct":-0.10,
            "ask_liquidity_change_pct":0.05,
            "cancellation_rate_10s":0.90,
            "bid_cancel_quote_10s":3000,
            "ask_cancel_quote_10s":500,
        },max_cancellation_rate=0.75,bid_cancel_imbalance_ratio=1.5)
        self.assertFalse(x["ok"])
        self.assertIn("BID_CANCELLATION_SPIKE",x["reasons"])


    def test_new_listing_quarantine(self):
        now=10*86_400_000
        too_new=[{"t":8*86_400_000},{"t":9*86_400_000}]
        x=listing_age_status(too_new,min_complete_days=3,now_ms=now)
        self.assertFalse(x["ok"])
        self.assertEqual(x["reason"],"NEW_LISTING_QUARANTINE")

        mature=[
            {"t":5*86_400_000},{"t":6*86_400_000},{"t":7*86_400_000},
            {"t":8*86_400_000},{"t":9*86_400_000},
        ]
        y=listing_age_status(mature,min_complete_days=3,now_ms=now)
        self.assertTrue(y["ok"])
        self.assertGreaterEqual(y["age_days"],3)


if __name__=="__main__":
    unittest.main()
