import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/"freqtrade"))

def test_guard_uses_canonical_state_path():
 text=(Path(__file__).parents[1]/"freqtrade"/"production_runtime_guard.py").read_text()
 assert '/data/tst_live_positions.json' in text
 assert '/data/trade_state.json' not in text

def test_capacity_is_fresh_execution_time_data():
 text=(Path(__file__).parents[1]/"freqtrade"/"front_proxy.py").read_text()
 assert "/api/v3/depth?symbol=" in text
 assert "/api/v3/trades?symbol=" in text
 assert "CAPACITY_LIVE_DATA_UNAVAILABLE" in text
 assert "body['depth_near_touch_usdt']" in text
 assert "body['recent_trade_flow_usdt']" in text

def test_single_position_does_not_require_correlation_history():
 import deep_readiness_runtime as d
 text=Path(d.__file__).read_text()
 assert 'factor_required=len(positions)>=2' in text
 assert 'NOT_APPLICABLE_SINGLE_POSITION' in text
