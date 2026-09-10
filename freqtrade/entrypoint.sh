#!/usr/bin/env bash
set -euo pipefail

# Production priority: keep the live signal/execution path alive. The full NFI
# Freqtrade dry-run is useful for shadow comparison, but it is memory-heavy and
# must never be allowed to OOM-kill the Telegram/fast-entry service.
SIGNAL_FIRST_MODE="${SIGNAL_FIRST_MODE:-1}"

if [[ "$SIGNAL_FIRST_MODE" == "1" ]]; then
  echo "[entrypoint] SIGNAL_FIRST_MODE=1; skipping memory-heavy NFI/Freqtrade dry-run"
  sleep infinity &
  BOT_PID=$!
else
  /freqtrade/run_ready_bot.sh &
  BOT_PID=$!
fi

cleanup() {
  kill "${EV_VALIDATION_PID:-}" "${SHADOW_EV_PID:-}" "${SHADOW_RESEARCH_PID:-}" "${MARKET_CONTEXT_PID:-}" "${RECOVERY_PID:-}" "${RECONCILE_PID:-}" "${DYNAMIC_EXIT_PID:-}" "${OUTCOME_ENGINE_PID:-}" "${SOL_MONITOR_PID:-}" "${NEW_LISTING_PID:-}" "${FAST_PID:-}" "$BOT_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

# telegram_signal_bridge.py is baked into the image. In legacy mode run_ready_bot
# may refresh it, but signal-first mode does not need that heavy bootstrap.
for _ in $(seq 1 20); do
  if [[ -s /freqtrade/telegram_signal_bridge.py ]]; then
    break
  fi
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    wait "$BOT_PID"
    exit $?
  fi
  sleep 1
done

if [[ ! -s /freqtrade/telegram_signal_bridge.py ]]; then
  echo "[entrypoint] telegram bridge did not become ready" >&2
  wait "$BOT_PID"
  exit $?
fi

sleep 1

if [[ "${TELEGRAM_STARTUP_TEST:-0}" == "1" ]]; then
  python - <<'PY'
import sys
sys.path.insert(0, '/freqtrade')
import telegram_signal_bridge as bridge
try:
    bridge.tg_api('sendMessage', {
        'text': '✅ TST Spot Sniper ONLINE\nTelegram connected successfully.\nLive market scanner is running.',
        'disable_web_page_preview': True,
    })
    print('[telegram-test] SENT successfully', flush=True)
except Exception as exc:
    print(f'[telegram-test] FAILED {type(exc).__name__}: {exc}', flush=True)
PY
fi

if ! python -u /freqtrade/reconcile_state.py --once; then
  echo "[entrypoint] CRITICAL reconciliation failed; fast live entry engine remains disabled" >&2
  wait "$BOT_PID"
  exit $?
fi

echo "[entrypoint] startup reconciliation passed"
python -u /freqtrade/reconcile_state.py &
RECONCILE_PID=$!
echo "[entrypoint] Binance reconciler started pid=${RECONCILE_PID}"

python -u /freqtrade/execution_recovery.py &
RECOVERY_PID=$!
echo "[entrypoint] exact-once execution recovery started pid=${RECOVERY_PID}"

if python -u /freqtrade/market_context.py --once; then
  echo "[entrypoint] initial market-context snapshot ready"
else
  echo "[entrypoint] market-context preflight unavailable; continuing in EV warmup mode" >&2
fi
python -u /freqtrade/market_context.py &
MARKET_CONTEXT_PID=$!
echo "[entrypoint] market-context collector started pid=${MARKET_CONTEXT_PID}"

# This is the primary production workload. Start it before all research workers.
python -u /freqtrade/fast_entry_engine.py &
FAST_PID=$!
echo "[entrypoint] Spot Sniper fast entry engine started pid=${FAST_PID}"

python -u /freqtrade/dynamic_exit_manager.py &
DYNAMIC_EXIT_PID=$!
echo "[entrypoint] ownership-safe dynamic exit manager started pid=${DYNAMIC_EXIT_PID}"

python -u /freqtrade/sol_buy_zone_watch.py &
SOL_MONITOR_PID=$!
echo "[entrypoint] SOL buy-zone Telegram monitor started pid=${SOL_MONITOR_PID}"

if [[ -n "${NEW_LISTING_SYMBOL:-}" && -n "${NEW_LISTING_START_UTC:-}" ]]; then
  python -u /freqtrade/new_listing_watcher.py &
  NEW_LISTING_PID=$!
  echo "[entrypoint] new listing watcher started pid=${NEW_LISTING_PID} symbol=${NEW_LISTING_SYMBOL} start=${NEW_LISTING_START_UTC}"
else
  echo "[entrypoint] new listing watcher disabled: schedule not configured"
fi

# Research remains enabled, but it starts after the signal path so an OOM issue
# can be isolated without ever sacrificing signal delivery again.
if [[ "${ENABLE_RESEARCH_WORKERS:-1}" == "1" ]]; then
  python -u /freqtrade/outcome_engine.py &
  OUTCOME_ENGINE_PID=$!
  echo "[entrypoint] forward outcome engine started pid=${OUTCOME_ENGINE_PID}"

  python -u /freqtrade/shadow_research_monitor.py &
  SHADOW_RESEARCH_PID=$!
  echo "[entrypoint] shadow research evidence monitor started pid=${SHADOW_RESEARCH_PID}"

  python -u /freqtrade/shadow_ev_model.py &
  SHADOW_EV_PID=$!
  echo "[entrypoint] calibrated probability/EV model trainer started pid=${SHADOW_EV_PID}"

  python -u /freqtrade/ev_validation_guard.py &
  EV_VALIDATION_PID=$!
  echo "[entrypoint] EV walk-forward promotion guard started pid=${EV_VALIDATION_PID}"
else
  echo "[entrypoint] research workers disabled; signal/execution path remains active"
fi

# Keep the container alive while continuously supervising the primary fast engine.
while true; do
  if ! kill -0 "$FAST_PID" 2>/dev/null; then
    echo "[entrypoint] CRITICAL fast-entry engine exited; terminating for clean Railway restart" >&2
    wait "$FAST_PID" || true
    exit 1
  fi
  if ! kill -0 "$RECONCILE_PID" 2>/dev/null; then
    echo "[entrypoint] CRITICAL reconciler exited; fail-closed restart" >&2
    wait "$RECONCILE_PID" || true
    exit 1
  fi
  sleep 15
done
