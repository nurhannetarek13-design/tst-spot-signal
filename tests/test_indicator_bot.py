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
        this_qv=qv*(1.6 if i==n-1 else 1.0)
        out.append({"t":i*900000,"o":o,"h":h,"l":l,"c":p,"v":this_qv/p,"qv":this_qv,"tq":this_qv*taker})
    return out


def spot_filters(symbol="TESTUSDT", verified=True):
    return {
        "min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,
        "max_qty":1e9,"step_size":0.0001,
        "spot_verified":verified,"quote_asset":"USDT","symbol":symbol,
    }


class IndicatorBotTests(unittest.TestCase):
    def test_engine_contract_is_spot_paper_5_of_6(self):
        self.assertEqual(bot.CFG["mode"],"paper")
        self.assertEqual(bot.CFG["market_type"],"spot")
        self.assertEqual(bot.CFG["engine"],"INDICATOR_ONLY_V2_5OF6")
        self.assertEqual(bot.CFG["entry"]["score_required"],5)
        self.assertEqual(bot.CFG["entry"]["score_total"],6)
        self.assertEqual(bot.CFG["entry"]["adx_min"],20)
        self.assertEqual(bot.CFG["entry"]["rsi_min"],52)
        self.assertEqual(bot.CFG["entry"]["rsi_max"],68)
        self.assertEqual(bot.CFG["entry"]["min_relative_quote_volume"],1.3)
        self.assertEqual(bot.CFG["entry"]["min_taker_buy_ratio"],0.55)
        self.assertEqual(bot.CFG["risk"]["max_risk_per_trade_fraction"],0.005)
        self.assertFalse(bot.CFG["risk"]["allow_averaging_down"])
        self.assertFalse(bot.CFG["risk"]["allow_martingale"])
        self.assertEqual(bot.CFG["risk"]["leverage"],1)

    def test_six_indicator_snapshot_contains_exact_checks(self):
        snap=bot.indicator_snapshot(
            "TESTUSDT",bars(taker=.62),bars(drift=.0015,taker=.58),
            bars(drift=.002,taker=.58),1.0,1.0005,50_000_000
        )
        self.assertEqual(set(snap["checks"]),{
            "ema_20_gt_50_15m","adx_14","rsi_14","rvol_20","taker_buy_ratio","vwap_retest"
        })
        self.assertGreaterEqual(snap["score"],0)
        self.assertLessEqual(snap["score"],6)
        self.assertEqual(snap["score_total"],6)
        self.assertIsNotNone(snap["adx_15m"])
        self.assertIsNotNone(snap["vwap_15m"])
        self.assertIsNotNone(snap["atr_15m"])

    def test_taker_under_50_is_hard_long_reject(self):
        snap=bot.indicator_snapshot(
            "TESTUSDT",bars(taker=.45),bars(drift=.0015),bars(drift=.002),
            1.0,1.0005,50_000_000
        )
        self.assertIn("TAKER_FLOW_LT_50",snap["vetoes"])
        self.assertFalse(snap["eligible"])

    def test_1h_close_below_ema200_is_hard_gate(self):
        snap=bot.indicator_snapshot(
            "TESTUSDT",bars(drift=.001,taker=.62),bars(drift=-.002,taker=.60),
            bars(drift=.002),1.0,1.0005,50_000_000
        )
        self.assertIn("EMA200_1H",snap["vetoes"])
        self.assertFalse(snap["eligible"])

    def test_vwap_retest_requires_price_above_and_touch(self):
        b=bars(n=40,drift=.0002)
        vs=bot.session_vwap_series(b)
        self.assertTrue(bot.successful_vwap_retest(b,vs,lookback=4,tolerance_pct=.01))
        b[-1]["c"]=vs[-1]*.99
        self.assertFalse(bot.successful_vwap_retest(b,bot.session_vwap_series(b),lookback=4,tolerance_pct=.01))

    def test_confirmed_swing_low_returns_pivot(self):
        b=bars(n=40,drift=.0002)
        b[-8]["l"]=min(x["l"] for x in b[-20:])*.95
        pivot=bot.confirmed_swing_low(b,lookback=20,left=2,right=2)
        self.assertAlmostEqual(pivot,b[-8]["l"])

    def test_open_position_uses_swing_and_atr_and_risk_limits(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0}
        snap={
            "symbol":"TESTUSDT","ask":1.0,"bar_time":1,"score":5,"score_total":6,
            "checks":{"ema_20_gt_50_15m":True},"eligible":True,"vetoes":[],
            "atr_15m":0.005,"confirmed_swing_low":0.99,
        }
        self.assertEqual(bot.open_position(state,snap,spot_filters()),"PAPER_OPENED")
        p=state["positions"]["TESTUSDT"]
        self.assertLess(p["stop"],snap["confirmed_swing_low"])
        allowed=min(
            bot.CFG["risk"]["max_risk_per_trade_usdt"],
            20.08*bot.CFG["risk"]["max_risk_per_trade_fraction"],
        )
        self.assertLessEqual(bot.stop_risk(p),allowed+1e-9)
        self.assertTrue(0 < p["stop"] < p["entry"] < p["target"])

    def test_abnormally_wide_swing_stop_is_skipped(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0}
        snap={
            "symbol":"TESTUSDT","ask":1.0,"bar_time":1,"score":5,"score_total":6,
            "checks":{},"eligible":True,"vetoes":[],
            "atr_15m":0.01,"confirmed_swing_low":0.90,
        }
        self.assertEqual(bot.open_position(state,snap,spot_filters()),"STOP_TOO_WIDE")

    def test_daily_loss_gate_blocks_new_position(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":-2.0}
        snap={
            "symbol":"TESTUSDT","ask":1.0,"bar_time":1,"score":5,"score_total":6,
            "checks":{},"eligible":True,"vetoes":[],
            "atr_15m":0.005,"confirmed_swing_low":0.99,
        }
        self.assertEqual(bot.open_position(state,snap,spot_filters()),"DAILY_LOSS_CAP")

    def test_spot_symbol_record_is_fail_closed(self):
        good={"status":"TRADING","quoteAsset":"USDT","baseAsset":"SOL","isSpotTradingAllowed":True,"permissionSets":[["SPOT"]]}
        self.assertTrue(bot.is_spot_symbol_record(good))
        missing=dict(good);missing.pop("isSpotTradingAllowed")
        self.assertFalse(bot.is_spot_symbol_record(missing))
        non_spot=dict(good);non_spot["permissionSets"]=[["MARGIN"]]
        self.assertFalse(bot.is_spot_symbol_record(non_spot))
        wrong=dict(good);wrong["quoteAsset"]="USDC"
        self.assertFalse(bot.is_spot_symbol_record(wrong))

    def test_non_spot_execution_and_routes_are_rejected(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0}
        snap={
            "symbol":"TESTUSDT","ask":1.0,"bar_time":1,"score":5,"score_total":6,
            "checks":{},"eligible":True,"vetoes":[],
            "atr_15m":0.005,"confirmed_swing_low":0.99,
        }
        self.assertEqual(bot.open_position(state,snap,spot_filters(verified=False)),"SPOT_ONLY_VIOLATION")
        with self.assertRaisesRegex(RuntimeError,"SPOT_ONLY_ROUTE_VIOLATION"):
            bot.market("/fapi/v1/klines?symbol=BTCUSDT")

    def test_execution_revalidates_signal_and_symbol(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0}
        bad={
            "symbol":"TESTUSDT","ask":1.0,"bar_time":1,"score":5,"score_total":6,
            "checks":{},"eligible":False,"vetoes":["RSI_LATE"],
            "atr_15m":0.005,"confirmed_swing_low":0.99,
        }
        self.assertEqual(bot.open_position(state,bad,spot_filters()),"SIGNAL_NOT_ELIGIBLE")
        good=dict(bad);good["eligible"]=True;good["vetoes"]=[]
        self.assertEqual(bot.open_position(state,good,spot_filters("BTCUSDT")),"SYMBOL_FILTER_MISMATCH")


if __name__=="__main__":
    unittest.main()
