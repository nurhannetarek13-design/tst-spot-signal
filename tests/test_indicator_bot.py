import unittest

import ready_bot.indicator_bot as bot


def bars(n=250, drift=0.001, taker=0.60, qv=2_000_000.0):
    out=[]
    p=1.0
    for i in range(n):
        o=p
        p=p*(1+drift)
        h=max(o,p)*1.002
        l=min(o,p)*0.998
        this_qv=qv*(1.7 if i==n-1 else 1.0)
        out.append({"t":i*900000,"o":o,"h":h,"l":l,"c":p,"v":this_qv/p,"qv":this_qv,"tq":this_qv*taker})
    return out


class IndicatorBotTests(unittest.TestCase):
    def test_engine_is_indicator_only_and_paper(self):
        self.assertEqual(bot.CFG["mode"],"paper")
        self.assertEqual(bot.CFG["market_type"],"spot")
        self.assertEqual(bot.CFG["engine"],"INDICATOR_ONLY_V1")
        self.assertNotIn("strategies",bot.CFG)
        self.assertEqual(bot.CFG["entry"]["min_score"],85)
        self.assertTrue(bot.CFG["evidence_gate"]["required"])
        self.assertEqual(bot.CFG["evidence_gate"]["minimum_win_rate"],0.99)
        self.assertEqual(bot.CFG["evidence_gate"]["scope"],"live_only")
        self.assertTrue(bot.CFG["evidence_gate"]["paper_observation_allowed"])

    def test_indicator_score_is_bounded_and_grouped(self):
        snap=bot.indicator_snapshot("TESTUSDT",bars(taker=.62),bars(drift=.0015,taker=.58),
                                    bars(drift=.002,taker=.58),1.0,1.0005,50_000_000)
        self.assertGreaterEqual(snap["score"],0)
        self.assertLessEqual(snap["score"],100)
        self.assertEqual(set(snap["groups"]),{"trend","momentum","flow","volatility","liquidity"})
        self.assertLess(snap["spread_bps"],bot.CFG["entry"]["max_spread_bps"])

    def test_low_taker_flow_is_hard_veto(self):
        snap=bot.indicator_snapshot("TESTUSDT",bars(taker=.45),bars(drift=.0015,taker=.58),
                                    bars(drift=.002,taker=.58),1.0,1.0005,50_000_000)
        self.assertIn("TAKER_FLOW",snap["vetoes"])
        self.assertFalse(snap["eligible"])

    def test_btc_regime_keeps_macro_guard_without_requiring_1h_bullish(self):
        self.assertTrue(bot.btc_regime(bars(drift=.001),bars(drift=.002))["ok"])
        soft=bot.btc_regime(bars(drift=-.0002),bars(drift=.002))
        self.assertTrue(soft["macroBullish"])
        self.assertFalse(soft["oneHourBullish"])
        self.assertTrue(soft["ok"])
        crash=bot.btc_regime(bars(drift=-.02),bars(drift=.002))
        self.assertFalse(crash["ok"])

    def test_open_position_sizes_by_risk_and_never_exceeds_limits(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0}
        snap={"symbol":"TESTUSDT","ask":1.0,"atr_pct_1h":0.02,"bar_time":1,
              "score":95,"groups":{"trend":25,"momentum":20,"flow":30,"volatility":10,"liquidity":10}}
        filters={"min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,"max_qty":1e9,"step_size":0.0001,"spot_verified":True,"quote_asset":"USDT"}
        self.assertEqual(bot.open_position(state,snap,filters),"PAPER_OPENED")
        p=state["positions"]["TESTUSDT"]
        self.assertLessEqual(p["cost"],bot.CFG["risk"]["max_quote_per_trade_usdt"]*(1+bot.CFG["risk"]["fee_rate"])+0.01)
        self.assertLessEqual(bot.stop_risk(p),bot.CFG["risk"]["max_risk_per_trade_usdt"]+1e-9)
        self.assertTrue(0 < p["stop"] < p["entry"] < p["target"])

    def test_daily_loss_gate_blocks_new_position(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":-2.0}
        snap={"symbol":"TESTUSDT","ask":1.0,"atr_pct_1h":0.02,"bar_time":1,"score":95,"groups":{}}
        filters={"min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,"max_qty":1e9,"step_size":0.0001,"spot_verified":True,"quote_asset":"USDT"}
        self.assertEqual(bot.open_position(state,snap,filters),"DAILY_LOSS_CAP")


    def test_precision_evidence_gate_fails_closed_on_current_report(self):
        status=bot.precision_evidence_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"],"PRECISION_EVIDENCE_NOT_MET")
        self.assertEqual(status["requiredWinRate"],0.99)
        self.assertGreaterEqual(status["requiredTrades"],100)
        self.assertFalse(bot.evidence_blocks_entries(status))


    def test_spot_symbol_record_is_fail_closed(self):
        good={"status":"TRADING","quoteAsset":"USDT","baseAsset":"SOL","isSpotTradingAllowed":True,"permissions":["SPOT"]}
        self.assertTrue(bot.is_spot_symbol_record(good))
        missing_flag=dict(good); missing_flag.pop("isSpotTradingAllowed")
        self.assertFalse(bot.is_spot_symbol_record(missing_flag))
        futures_like=dict(good); futures_like["permissions"]=["TRD_GRP_004"]
        self.assertFalse(bot.is_spot_symbol_record(futures_like))
        wrong_quote=dict(good); wrong_quote["quoteAsset"]="USDC"
        self.assertFalse(bot.is_spot_symbol_record(wrong_quote))

    def test_open_position_rejects_unverified_non_spot_symbol(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0}
        snap={"symbol":"TESTUSDT","ask":1.0,"atr_pct_1h":0.02,"bar_time":1,"score":95,"groups":{}}
        filters={"min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,"max_qty":1e9,"step_size":0.0001,"spot_verified":False,"quote_asset":"USDT"}
        self.assertEqual(bot.open_position(state,snap,filters),"SPOT_ONLY_VIOLATION")

    def test_market_rejects_non_spot_api_routes_before_network(self):
        with self.assertRaisesRegex(RuntimeError,"SPOT_ONLY_ROUTE_VIOLATION"):
            bot.market("/fapi/v1/klines?symbol=BTCUSDT")


if __name__=="__main__":
    unittest.main()
