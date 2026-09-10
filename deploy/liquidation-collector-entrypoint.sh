#!/bin/sh
set -eu

COINALYZE_OUT="${COINALYZE_DATA_DIR:-/data/coinalyze-liquidations}"
COINALYZE_DAYS="${COINALYZE_BACKFILL_DAYS:-365}"
COINALYZE_INTERVAL="${COINALYZE_INTERVAL:-daily}"
RUN_STARTUP_RESEARCH="${RUN_STARTUP_RESEARCH:-0}"

python /app/binance_public_proxy.py &
PROXY_PID=$!

# Time-sensitive forward collectors start immediately. Historical research is
# optional and must never create a collection blind spot.
python /app/forward_liquidation_collector.py &
COLLECTOR_PID=$!
python /app/forward_microstructure_collector_v1.py &
MICRO_PID=$!

RESEARCH_PID=""
cleanup() {
  [ -z "${RESEARCH_PID:-}" ] || kill "$RESEARCH_PID" 2>/dev/null || true
  kill "$MICRO_PID" 2>/dev/null || true
  kill "$COLLECTOR_PID" 2>/dev/null || true
  kill "$PROXY_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

run_research_tasks() {
  if [ -n "${COINALYZE_API_KEY:-}" ]; then
    echo "{\"kind\":\"coinalyze_backfill_start\",\"authorization\":\"RESEARCH_ONLY\",\"days\":${COINALYZE_DAYS},\"interval\":\"${COINALYZE_INTERVAL}\",\"outputDir\":\"${COINALYZE_OUT}\"}"
    python /app/coinalyze_liquidation_history.py --days "${COINALYZE_DAYS}" --interval "${COINALYZE_INTERVAL}" --output-dir "${COINALYZE_OUT}" || true
    python /app/coinalyze_daily_regime_discovery.py || true
    python /app/binance_vision_extreme_shock_validation.py || true
    python /app/extreme_shock_frozen_oos.py || true
    python /app/extreme_shock_frozen_multiyear.py || true
  else
    echo "{\"kind\":\"coinalyze_backfill_skipped\",\"reason\":\"missing_api_key\",\"authorization\":\"RESEARCH_ONLY\"}"
  fi

  if [ "${TV_BREAKOUT_RUN:-0}" = "1" ]; then
    echo "{\"kind\":\"tv_breakout_raw_gate_start\",\"authorization\":\"RESEARCH_ONLY\"}"
    python /app/tradingview_breakout_raw_gate.py || true
  fi
}

if [ "$RUN_STARTUP_RESEARCH" = "1" ]; then
  run_research_tasks &
  RESEARCH_PID=$!
else
  echo "{\"kind\":\"startup_research_skipped\",\"authorization\":\"RESEARCH_ONLY\",\"reason\":\"disabled_after_completed_validation\"}"
fi

# The liquidation collector remains the service lifecycle anchor. The
# microstructure collector has its own retry loop and is terminated with it.
wait "$COLLECTOR_PID"
