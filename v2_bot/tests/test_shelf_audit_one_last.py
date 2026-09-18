from v2_bot.crash_fvg_profit_audit import SYMBOLS

def test_only_spot_pairs():
    assert all(s.endswith('USDT') for s in SYMBOLS)
