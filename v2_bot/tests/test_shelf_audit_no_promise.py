from v2_bot.crash_fvg_profit_audit import ASSUMED_FULL_SPREAD

def test_historical_book_is_assumption():
    assert ASSUMED_FULL_SPREAD > 0
