#!/bin/sh
set -eu

COINALYZE_OUT="${COINALYZE_DATA_DIR:-/data/coinalyze-liquidations}"
COINALYZE_DAYS="${COINALYZE_BACKFILL_DAYS:-365}"
COINALYZE_INTERVAL="${COINALYZE_INTERVAL:-daily}"

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

exec python /app/forward_liquidation_collector.py
