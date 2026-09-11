import unittest
from decimal import Decimal

from v2_bot.spot_preflight import SpotSymbolRules, validate_protected_spot_trade


def symbol_info(min_notional="5.0", step_size="0.001", tick_size="0.01"):
    return {
        "symbol": "TESTUSDT",
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": tick_size,
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "100000",
                "stepSize": step_size,
            },
            {
                "filterType": "MIN_NOTIONAL",
                "minNotional": min_notional,
            },
        ],
    }


class SpotPreflightTests(unittest.TestCase):
    def test_clean_trade_all_three_legs_pass(self):
        rules = SpotSymbolRules.from_exchange_info(symbol_info())
        result = validate_protected_spot_trade(
            rules=rules,
            quote_size=10,
            entry_price=100,
            take_profit_price=100.9,
            stop_loss_price=99.38,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.quantity, Decimal("0.1"))
        self.assertGreaterEqual(result.entry_notional, Decimal("5"))
        self.assertGreaterEqual(result.take_profit_notional, Decimal("5"))
        self.assertGreaterEqual(result.stop_loss_notional, Decimal("5"))

    def test_rejects_before_buy_when_stop_leg_would_fall_below_min_notional(self):
        rules = SpotSymbolRules.from_exchange_info(symbol_info(min_notional="5"))
        result = validate_protected_spot_trade(
            rules=rules,
            quote_size=5,
            entry_price=100,
            take_profit_price=101,
            stop_loss_price=99,
        )

        self.assertFalse(result.allowed)
        self.assertIn("stop_loss_notional_below_min", result.reasons)
        self.assertNotIn("entry_notional_below_min", result.reasons)

    def test_step_rounding_can_make_entry_and_exit_notional_invalid(self):
        rules = SpotSymbolRules.from_exchange_info(
            symbol_info(min_notional="5", step_size="0.01")
        )
        result = validate_protected_spot_trade(
            rules=rules,
            quote_size=5.05,
            entry_price=101,
            take_profit_price=102,
            stop_loss_price=100,
        )

        self.assertEqual(result.quantity, Decimal("0.05"))
        self.assertTrue(
            any(reason.endswith("notional_below_min") for reason in result.reasons)
        )
        self.assertFalse(result.allowed)

    def test_notional_filter_uses_stricter_minimum(self):
        info = symbol_info(min_notional="5")
        info["filters"].append(
            {
                "filterType": "NOTIONAL",
                "minNotional": "8",
                "maxNotional": "1000",
            }
        )
        rules = SpotSymbolRules.from_exchange_info(info)
        self.assertEqual(rules.min_notional, Decimal("8"))
        self.assertEqual(rules.max_notional, Decimal("1000"))

    def test_rejects_non_trading_symbol(self):
        info = symbol_info()
        info["status"] = "BREAK"
        with self.assertRaisesRegex(ValueError, "not TRADING"):
            SpotSymbolRules.from_exchange_info(info)


if __name__ == "__main__":
    unittest.main()
