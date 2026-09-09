#!/usr/bin/env bash
set -euo pipefail

# Deployment marker: production-safety state + reconciliation + Spot Sniper research/ops
/freqtrade/run_ready_bot.sh &
BOT_PID=$!

cleanup() {
  kill "${EV_VALIDATION_PID:-}" "${SHADOW_EV_PID:-}" "${SHADOW_RESEARCH_PID:-}" "${MARKET_CONTEXT_PID:-}" "${RECOVERY_PID:-}" "${RECONCILE_PID:-}" "${DYNAMIC_EXIT_PID:-}" "${OUTCOME_ENGINE_PID:-}" "${SOL_MONITOR_PID:-}" "${NEW_LISTING_PID:-}" "${FAST_PID:-}" "$BOT_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

for _ in $(seq 1 60); do
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

sleep 3

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

# Fail closed: no new live opportunity may be emitted until the canonical
# persistent ledger is reconciled against Binance open bot-owned OCOs.
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

# Point-in-time regime + cross-sectional context. Panic is a safety block now;
# ranking becomes a hard live filter only after the EV validation guard approves.
if python -u /freqtrade/market_context.py --once; then
  echo "[entrypoint] initial market-context snapshot ready"
else
  echo "[entrypoint] market-context preflight unavailable; continuing in EV warmup mode" >&2
fi
python -u /freqtrade/market_context.py &
MARKET_CONTEXT_PID=$!
echo "[entrypoint] market-context collector started pid=${MARKET_CONTEXT_PID}"

python -u /freqtrade/fast_entry_engine.py &
FAST_PID=$!
echo "[entrypoint] Spot Sniper fast entry engine started pid=${FAST_PID}"

python -u /freqtrade/outcome_engine.py &
OUTCOME_ENGINE_PID=$!
echo "[entrypoint] forward outcome engine started pid=${OUTCOME_ENGINE_PID}"

python -u /freqtrade/shadow_research_monitor.py &
SHADOW_RESEARCH_PID=$!
echo "[entrypoint] shadow research evidence monitor started pid=${SHADOW_RESEARCH_PID}"

# The model trains only from completed point-in-time outcomes. It is not treated
# as a hard live gate until ev_validation_guard writes a matching APPROVED record.
python -u /freqtrade/shadow_ev_model.py &
SHADOW_EV_PID=$!
echo "[entrypoint] calibrated probability/EV model trainer started pid=${SHADOW_EV_PID}"

python -u /freqtrade/ev_validation_guard.py &
EV_VALIDATION_PID=$!
echo "[entrypoint] EV walk-forward promotion guard started pid=${EV_VALIDATION_PID}"

# Dynamic exit may request live mode, but it independently proves Binance TRADE
# write permission and exact OCO ownership before any cancel/replace. Otherwise
# it automatically remains shadow-only.
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

wait "$BOT_PID"
exit $?
