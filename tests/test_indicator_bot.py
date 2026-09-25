import os
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

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


def execution_depth(price=1.0):
    return {
        "bids":[[str(price*0.9999),"1000"]],
        "asks":[[str(price),"1000"],[str(price*1.0001),"1000"]],
    }


def spot_filters(symbol="TESTUSDT", verified=True):
    return {
        "min_notional":1.0,"max_notional":1e9,"min_qty":0.0001,
        "max_qty":1e9,"step_size":0.0001,"tick_size":0.0001,
        "spot_verified":verified,"quote_asset":"USDT","symbol":symbol,
    }


class IndicatorBotTests(unittest.TestCase):
    def test_engine_contract_is_spot_paper_5_of_6(self):
        self.assertEqual(bot.CFG["mode"],"paper")
        self.assertEqual(bot.CFG["market_type"],"spot")
        self.assertEqual(bot.CFG["engine"],"INDICATOR_ONLY_V3_EARLY_MOMENTUM")
        self.assertEqual(bot.CFG["entry"]["score_required"],5)
        self.assertEqual(bot.CFG["entry"]["score_total"],6)
        self.assertEqual(bot.CFG["entry"]["adx_min"],20)
        self.assertEqual(bot.CFG["entry"]["rsi_min"],52)
        self.assertEqual(bot.CFG["entry"]["rsi_max"],68)
        self.assertEqual(bot.CFG["entry"]["min_relative_quote_volume"],1.3)
        self.assertEqual(bot.CFG["entry"]["min_taker_buy_ratio"],0.55)
        self.assertEqual(bot.CFG["momentum"]["watch_min"],65)
        self.assertEqual(bot.CFG["momentum"]["armed_min"],75)
        self.assertEqual(bot.CFG["momentum"]["entry_candidate_min"],85)
        self.assertEqual(bot.CFG["momentum"]["obi_armed_min"],0.58)
        self.assertEqual(bot.CFG["risk"]["max_risk_per_trade_fraction"],0.005)
        self.assertFalse(bot.CFG["risk"]["allow_averaging_down"])
        self.assertFalse(bot.CFG["risk"]["allow_martingale"])
        self.assertEqual(bot.CFG["risk"]["leverage"],1)
        self.assertEqual(bot.CFG["exit"]["momentum_fade_min_age_minutes"],5)
        self.assertEqual(bot.CFG["exit"]["no_follow_through_minutes"],30)
        self.assertLessEqual(bot.CFG["production_guard"]["max_stream_signal_age_ms"],5000)

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
            "bid":0.9999,"spread_bps":1.0,
            "_execution_depth":execution_depth(1.0),
            "micro":{"score":90,"taker_rising":True,"agg_cvd":{"latest_event_ms":1}},
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


    def test_dynamic_exit_closes_when_momentum_fades(self):
        opened=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        state={
            "cash_usdt":10.0,"day_pnl":0.0,"closed_trades":[],
            "positions":{"TESTUSDT":{
                "entry":1.0,"stop":0.90,"target":1.20,"initial_risk_abs":0.10,
                "breakeven":False,"opened_at":opened,"qty":1.0,"cost":1.001,
                "peak_price":1.0,"trough_price":1.0,"mfe_r":0.0,"mae_r":0.0,
                "score":5
            }}
        }
        snap={"bid":0.99,"atr_15m":0.01,"micro_pre":{"taker_latest":0.40,"cvd_positive":False,"vwap":1.0}}
        bot.manage_position(state,"TESTUSDT",snap)
        self.assertNotIn("TESTUSDT",state["positions"])
        self.assertEqual(state["closed_trades"][-1]["reason"],"MOMENTUM_FADE")
        self.assertEqual(state["decision_log"][-1]["code"],"WHY_EXIT")
        self.assertEqual(state["decision_log"][-1]["reason"],"MOMENTUM_FADE")

    def test_dynamic_exit_frees_stalled_trade(self):
        opened=(datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat()
        state={
            "cash_usdt":10.0,"day_pnl":0.0,"closed_trades":[],
            "positions":{"TESTUSDT":{
                "entry":1.0,"stop":0.90,"target":1.20,"initial_risk_abs":0.10,
                "breakeven":False,"opened_at":opened,"qty":1.0,"cost":1.001,
                "peak_price":1.01,"trough_price":0.99,"mfe_r":0.10,"mae_r":0.10,
                "score":5
            }}
        }
        snap={"bid":1.005,"atr_15m":0.01,"micro_pre":{"taker_latest":0.55,"cvd_positive":True,"vwap":1.0}}
        bot.manage_position(state,"TESTUSDT",snap)
        self.assertNotIn("TESTUSDT",state["positions"])
        self.assertEqual(state["closed_trades"][-1]["reason"],"TIME_NO_FOLLOW_THROUGH")
        self.assertEqual(state["decision_log"][-1]["code"],"WHY_EXIT")
        self.assertEqual(state["decision_log"][-1]["reason"],"TIME_NO_FOLLOW_THROUGH")


    def test_duplicate_signal_id_is_idempotent(self):
        state={"cash_usdt":20.08,"positions":{},"day_pnl":0.0,"executed_signal_ids":{}}
        snap={
            "symbol":"TESTUSDT","ask":1.0,"bid":0.9999,"spread_bps":1.0,
            "bar_time":60_000,"score":5,"score_total":6,"checks":{},
            "eligible":True,"vetoes":[],"atr_15m":0.005,"confirmed_swing_low":0.99,
            "_execution_depth":execution_depth(1.0),
            "micro":{"score":90,"taker_rising":True,"agg_cvd":{"latest_event_ms":60_000}},
        }
        first=bot.open_position(state,snap,spot_filters())
        self.assertEqual(first,"PAPER_OPENED")
        state["positions"].pop("TESTUSDT")
        state["cash_usdt"]=20.08
        second=bot.open_position(state,snap,spot_filters())
        self.assertEqual(second,"DUPLICATE_SIGNAL_ID")


    def test_stream_sidecar_is_fail_closed_and_parses_snapshot(self):
        healthy={
            "health":{"ok":True},
            "snapshot":{
                "synced":True,"fresh":True,"warmed":True,
                "obi":0.61,"spreadBps":2.0,
                "bidLiquidityQuote5":1000.0,"askLiquidityQuote5":800.0,
                "deltaQuote60s":500.0,"takerBuyRatio60s":0.59,
                "cvdSlopePositive10s":True,"tradeAgeMs":100.0,
            }
        }
        with patch.dict(os.environ,{"TST_STREAM_SHADOW_URL":"http://127.0.0.1:8080"}):
            with patch.object(bot,"request_json",return_value=healthy):
                x=bot.stream_shadow_micro_snapshot("SOLUSDT")
                self.assertEqual(x["obi"],0.61)
            with patch.object(bot,"request_json",return_value={"health":{"ok":False},"snapshot":{}}):
                with self.assertRaisesRegex(RuntimeError,"STREAM_SHADOW_NOT_HEALTHY"):
                    bot.stream_shadow_micro_snapshot("SOLUSDT")


    def test_signal_id_dedupes_same_market_minute_only(self):
        base={
            "symbol":"SOLUSDT","bar_time":900_000,
            "micro":{"score":90,"agg_cvd":{"latest_event_ms":120_100}},
        }
        same=dict(base)
        same["micro"]={"score":99,"agg_cvd":{"latest_event_ms":120_999}}
        nxt=dict(base)
        nxt["micro"]={"score":90,"agg_cvd":{"latest_event_ms":180_001}}
        self.assertEqual(bot.signal_id_for_snapshot(base),bot.signal_id_for_snapshot(same))
        self.assertNotEqual(bot.signal_id_for_snapshot(base),bot.signal_id_for_snapshot(nxt))

    def test_stream_samples_satisfy_microstructure_warmup_count(self):
        snap={
            "_bars1m":[{}]*60,
            "_bars3m":[{}]*30,
            "_bars15":[{}]*220,
            "_bars1h":[{}]*220,
            "micro_pre":{"prefilter_score":70},
            "guard_ok":True,
        }
        stream_row={
            "obi":0.60,"spreadBps":2.0,
            "bidLiquidityQuote5":1000.0,"askLiquidityQuote5":900.0,
            "micropriceBiasBps":0.2,"cancellationRate10s":0.2,
            "bidCancelQuote10s":10.0,"askCancelQuote10s":5.0,
            "deltaQuote60s":100.0,"takerBuyRatio60s":0.58,
            "cvdSlopePositive10s":True,"tradeAgeMs":100.0,
        }
        micro={"stage":"WATCH","score":70,"agg_cvd":{"delta_quote":100},"depth_flow":{}}
        with patch.dict(os.environ,{"TST_STREAM_SHADOW_URL":"http://shadow"}):
            with patch.object(bot,"stream_shadow_micro_snapshot",return_value=stream_row),                  patch.object(bot.time,"sleep",return_value=None),                  patch.object(bot,"combine_microstructure",return_value=micro),                  patch.object(bot,"signal_freshness_status",return_value={"ok":True}),                  patch.object(bot,"adverse_selection_status",return_value={"ok":True}),                  patch.object(bot,"liquidity_disappearance_status",return_value={"ok":True}),                  patch.object(bot,"warmup_status",wraps=bot.warmup_status) as warm:
                bot.enrich_microstructure("SOLUSDT",snap)
                self.assertEqual(warm.call_args.kwargs["depth_samples"],bot.CFG["momentum"]["obi_samples"])
                self.assertTrue(snap["micro"]["production_guard"]["warmup"]["ok"])


    def test_exit_uses_bid_depth_and_records_slippage(self):
        opened=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        state={
            "cash_usdt":10.0,"day_pnl":0.0,"closed_trades":[],
            "exit_slippage_model":{},
            "positions":{"TESTUSDT":{
                "entry":1.0,"stop":0.95,"target":1.10,"initial_risk_abs":0.05,
                "breakeven":False,"opened_at":opened,"qty":10.0,"cost":10.01,
                "peak_price":1.0,"trough_price":1.0,"mfe_r":0.0,"mae_r":0.0,
                "score":5
            }}
        }
        snap={
            "bid":0.94,"atr_15m":0.01,
            "_exit_depth":{"bids":[["0.94","5"],["0.93","10"]],"asks":[["0.95","10"]]},
            "micro_pre":{"taker_latest":0.40,"cvd_positive":False,"vwap":1.0}
        }
        bot.manage_position(state,"TESTUSDT",snap)
        self.assertNotIn("TESTUSDT",state["positions"])
        trade=state["closed_trades"][-1]
        self.assertEqual(trade["reason"],"STOP")
        self.assertEqual(trade["exit_execution"]["source"],"LIVE_DEPTH")
        self.assertGreater(trade["exit_slippage_bps"],0)
        self.assertEqual(state["exit_slippage_model"]["TESTUSDT"]["count"],1)


    def test_symbol_slippage_history_can_block_entry(self):
        state={
            "cash_usdt":20.08,"positions":{},"day_pnl":0.0,
            "executed_signal_ids":{},
            "slippage_model":{"TESTUSDT":{"count":3,"ewma_bps":20.0,"max_bps":22.0,"last_bps":20.0}},
        }
        snap={
            "symbol":"TESTUSDT","ask":1.0,"bid":0.9999,"spread_bps":1.0,
            "bar_time":60_000,"score":5,"score_total":6,"checks":{},
            "eligible":True,"vetoes":[],"atr_15m":0.005,"confirmed_swing_low":0.99,
            "_execution_depth":execution_depth(1.0),
            "micro":{"score":90,"taker_rising":True,"agg_cvd":{"latest_event_ms":60_000}},
        }
        self.assertEqual(bot.open_position(state,snap,spot_filters()),"SYMBOL_SLIPPAGE_MODEL_REJECT")


if __name__=="__main__":
    unittest.main()
