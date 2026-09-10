#!/bin/sh
set -eu

COINALYZE_OUT="${COINALYZE_DATA_DIR:-/data/coinalyze-liquidations}"
COINALYZE_DAYS="${COINALYZE_BACKFILL_DAYS:-365}"
COINALYZE_INTERVAL="${COINALYZE_INTERVAL:-daily}"

python /app/binance_public_proxy.py &
PROXY_PID=$!

# The forward collector is the time-sensitive process. Start it immediately so
# research backfills/replications never create a liquidation-data blind spot.
python /app/forward_liquidation_collector.py &
COLLECTOR_PID=$!

RESEARCH_PID=""
cleanup() {
  [ -z "${RESEARCH_PID:-}" ] || kill "$RESEARCH_PID" 2>/dev/null || true
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

run_research_tasks &
RESEARCH_PID=$!

# Keep the service lifecycle tied to the collector. If it exits, Railway's
# restart policy can recover it instead of leaving only research processes alive.
wait "$COLLECTOR_PID"
