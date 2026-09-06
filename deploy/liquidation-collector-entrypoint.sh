#!/bin/sh
set -eu

COINALYZE_OUT="${COINALYZE_DATA_DIR:-/data/coinalyze-liquidations}"
COINALYZE_DAYS="${COINALYZE_BACKFILL_DAYS:-365}"
COINALYZE_INTERVAL="${COINALYZE_INTERVAL:-daily}"

if [ -n "${COINALYZE_API_KEY:-}" ]; then
  echo "{\"kind\":\"coinalyze_backfill_start\",\"authorization\":\"RESEARCH_ONLY\",\"days\":${COINALYZE_DAYS},\"interval\":\"${COINALYZE_INTERVAL}\",\"outputDir\":\"${COINALYZE_OUT}\"}"
  if python /app/coinalyze_liquidation_history.py --days "${COINALYZE_DAYS}" --interval "${COINALYZE_INTERVAL}" --output-dir "${COINALYZE_OUT}"; then
    echo "{\"kind\":\"coinalyze_backfill_complete\",\"authorization\":\"RESEARCH_ONLY\"}"
  else
    echo "{\"kind\":\"coinalyze_backfill_failed\",\"authorization\":\"RESEARCH_ONLY\"}" >&2
  fi

  echo "{\"kind\":\"coinalyze_regime_discovery_start\",\"authorization\":\"RESEARCH_ONLY\"}"
  if python /app/coinalyze_daily_regime_discovery.py; then
    echo "{\"kind\":\"coinalyze_regime_discovery_complete_marker\",\"authorization\":\"RESEARCH_ONLY\"}"
  else
    echo "{\"kind\":\"coinalyze_regime_discovery_failed\",\"authorization\":\"RESEARCH_ONLY\"}" >&2
  fi

  echo "{\"kind\":\"binance_vision_extreme_shock_validation_start\",\"authorization\":\"RESEARCH_ONLY\"}"
  if python /app/binance_vision_extreme_shock_validation.py; then
    echo "{\"kind\":\"binance_vision_extreme_shock_validation_complete_marker\",\"authorization\":\"RESEARCH_ONLY\"}"
  else
    echo "{\"kind\":\"binance_vision_extreme_shock_validation_failed\",\"authorization\":\"RESEARCH_ONLY\"}" >&2
  fi

  echo "{\"kind\":\"frozen_extreme_shock_oos_start\",\"authorization\":\"RESEARCH_ONLY\"}"
  if python /app/extreme_shock_frozen_oos.py; then
    echo "{\"kind\":\"frozen_extreme_shock_oos_complete_marker\",\"authorization\":\"RESEARCH_ONLY\"}"
  else
    echo "{\"kind\":\"frozen_extreme_shock_oos_failed\",\"authorization\":\"RESEARCH_ONLY\"}" >&2
  fi
else
  echo "{\"kind\":\"coinalyze_backfill_skipped\",\"reason\":\"missing_api_key\",\"authorization\":\"RESEARCH_ONLY\"}"
fi

exec python /app/forward_liquidation_collector.py
