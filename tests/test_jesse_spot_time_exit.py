from pathlib import Path

def test_jesse_time_exit_cancels_reserved_spot_exits_before_market_liquidation():
    code=Path("jesse/strategies/UnifiedCandidateValidator/__init__.py").read_text()
    helper="def _cancel_spot_exit_reservations_before_market_close"
    assert helper in code
    block=code[code.index(helper):code.index("    def update_position", code.index(helper))]
    assert "self.broker.cancel_all_orders()" in block
    assert "self.stop_loss=None" in block
    assert "self._stop_loss=None" in block
    assert "self.take_profit=None" in block
    assert "self._take_profit=None" in block
    update=code[code.index("    def update_position"):]
    assert "self._cancel_spot_exit_reservations_before_market_close()" in update
    assert "self.liquidate()" in update

if __name__=="__main__":
    test_jesse_time_exit_cancels_reserved_spot_exits_before_market_liquidation()
    print("Jesse spot time-exit reservation regression test: OK")
