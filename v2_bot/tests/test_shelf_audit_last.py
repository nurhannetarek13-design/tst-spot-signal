from v2_bot.pionex_style import MODES

def test_modes_are_frozen():
    assert MODES==('spot_grid','fixed_dca','trend_breakout')
