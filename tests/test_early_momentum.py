import unittest

from ready_bot.early_momentum import (
    WEIGHTS,
    aggtrade_delta,
    combine_microstructure,
    depth_flow_metrics,
    depth_imbalance,
    depth_snapshot_metrics,
    micro_breakout_hold,
    prefilter_snapshot,
)

CFG={
    "ignore_below":65,
    "watch_min":65,
    "armed_min":75,
    "entry_candidate_min":85,
    "volume_acceleration_1m_watch":1.5,
    "volume_acceleration_1m_armed":1.8,
    "volume_acceleration_3m_armed":1.5,
    "trade_count_watch":1.3,
    "trade_count_armed":1.5,
    "taker_watch_min":0.53,
    "taker_entry_min":0.56,
    "taker_armed_min":0.57,
    "obi_watch_min":0.55,
    "obi_armed_min":0.58,
    "spread_stable_multiplier":1.05,
    "vwap_max_atr_distance":1.5,
    "breakout_proximity_atr":0.25,
    "volume_not_collapsing_ratio":0.60,
    "chase_move_atr":1.5,
    "recent_resistance_lookback_1m":20,
    "volatility_expansion_ratio":1.15,
    "price_velocity_lookback_1m":3,
}


def bars(n=50, qv=1000.0, taker=0.52, trade_count=100, drift=0.0002):
    out=[]
    p=100.0
    for i in range(n):
        o=p
        p=p*(1+drift)
        this_qv=qv
        this_taker=taker
        this_n=trade_count
        if i==n-3:
            this_taker=.52
        if i==n-2:
            this_taker=.56
        if i==n-1:
            this_qv=qv*2.0
            this_taker=.60
            this_n=int(trade_count*1.8)
        out.append({
            "t":i*60_000,"o":o,"h":max(o,p)*1.0005,"l":min(o,p)*.9995,"c":p,
            "v":this_qv/p,"qv":this_qv,"n":this_n,"tq":this_qv*this_taker,
        })
    return out


class EarlyMomentumTests(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(WEIGHTS.values()),100)

    def test_prefilter_detects_acceleration_and_taker_slope(self):
        b1=bars()
        b3=bars(n=30,qv=3000.0)
        b3[-1]["qv"]=6000.0
        b3[-1]["tq"]=3600.0
        x=prefilter_snapshot(b1,b3,CFG)
        self.assertGreaterEqual(x["rvol_1m"],1.8)
        self.assertGreaterEqual(x["trade_count_accel"],1.5)
        self.assertTrue(x["taker_rising"])
        self.assertGreaterEqual(x["taker_latest"],.57)
        self.assertIn("cvd_positive",x)

    def test_depth_imbalance_uses_top_five_quote_liquidity(self):
        d={"bids":[["100","2"],["99","2"],["98","2"],["97","2"],["96","2"]],
           "asks":[["101","1"],["102","1"],["103","1"],["104","1"],["105","1"]]}
        self.assertGreater(depth_imbalance(d,5),.60)

    def test_depth_flow_and_microprice_are_measured(self):
        first={"bids":[["100","2"],["99","1"]],"asks":[["101","2"],["102","1"]]}
        last={"bids":[["100","3"],["99","2"]],"asks":[["101","1"],["102","1"]]}
        a=depth_snapshot_metrics(first,2)
        b=depth_snapshot_metrics(last,2)
        flow=depth_flow_metrics([a,b])
        self.assertIsNotNone(a["microprice"])
        self.assertGreater(flow["bid_liquidity_change_pct"],0)
        self.assertLess(flow["ask_liquidity_change_pct"],0)
        self.assertGreater(flow["pressure_change"],0)

    def test_aggtrade_delta_recognizes_aggressive_buying(self):
        rows=[
            {"p":"100","q":"1","T":1000,"m":True},
            {"p":"100","q":"2","T":61000,"m":False},
            {"p":"100","q":"3","T":121000,"m":False},
        ]
        x=aggtrade_delta(rows,180000)
        self.assertGreater(x["delta_quote"],0)
        self.assertGreater(x["ratio"],.5)

    def test_micro_breakout_requires_break_and_hold(self):
        b=bars(n=30,drift=0.0)
        resistance=max(x["h"] for x in b[-23:-3])
        b[-2]["h"]=resistance*1.001
        b[-1]["c"]=resistance*1.0002
        x=micro_breakout_hold(b,20,.0015)
        self.assertTrue(x["ok"])

    def test_persistent_obi_and_flow_can_reach_entry_candidate(self):
        pre={
            "prefilter_score":70,
            "rvol_1m":2.0,"rvol_3m":1.7,"trade_count_accel":1.8,
            "taker_last3":[.52,.56,.60],"taker_rising":True,"taker_latest":.60,
            "cvd_positive":True,"ema9_slope_positive":True,"volatility_expansion":True,
            "vwap":100.0,"vwap_distance_atr":.4,"vwap_position_ok":True,"atr_1m":1.0,
            "breakout":{"resistance":101,"distance_atr":.2},"breakout_proximity_ok":True,
            "price_velocity":.004,"move_atr":.4,"volume_hold_ratio":.9,
            "micro_breakout_hold":{"ok":True,"resistance":101,"breakout":True,"hold":True},
        }
        agg={"delta_quote":1000.0,"ratio":.62,"slope_positive":True}
        x=combine_microstructure(pre,[.60,.61,.59],[8.0,7.5,7.0],agg,CFG)
        self.assertEqual(x["stage"],"ENTRY_CANDIDATE")
        self.assertGreaterEqual(x["score"],85)

    def test_orderbook_alone_cannot_force_entry(self):
        pre={
            "prefilter_score":70,
            "rvol_1m":2.0,"rvol_3m":1.7,"trade_count_accel":1.8,
            "taker_last3":[.60,.58,.55],"taker_rising":False,"taker_latest":.55,
            "cvd_positive":False,"ema9_slope_positive":True,"volatility_expansion":True,
            "vwap":100.0,"vwap_distance_atr":.4,"vwap_position_ok":True,"atr_1m":1.0,
            "breakout":{"resistance":101,"distance_atr":.2},"breakout_proximity_ok":True,
            "price_velocity":.004,"move_atr":.4,"volume_hold_ratio":.9,
            "micro_breakout_hold":{"ok":True,"resistance":101,"breakout":True,"hold":True},
        }
        agg={"delta_quote":-1000.0,"ratio":.45,"slope_positive":False}
        x=combine_microstructure(pre,[.65,.64,.63],[7.0,7.0,7.0],agg,CFG)
        self.assertNotEqual(x["stage"],"ENTRY_CANDIDATE")


if __name__=="__main__":
    unittest.main()
