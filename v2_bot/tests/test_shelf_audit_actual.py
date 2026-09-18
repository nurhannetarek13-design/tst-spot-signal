from v2_bot.pionex_style import snapshot,new_state

def test_no_live_exchange_routing():
    s=snapshot(new_state('spot_grid','BTCUSDT'), {'bid':100,'ask':101},'hold')
    assert s['actual_orders']==0 and s['live_trading'] is False
