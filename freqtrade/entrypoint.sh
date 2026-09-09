#!/usr/bin/env bash
set -euo pipefail

# Deployment marker: momentum growth + dedicated fail-closed new-listing watcher v1
/freqtrade/run_ready_bot.sh &
BOT_PID=$!

cleanup() {
  kill "${NEW_LISTING_PID:-}" "${FAST_PID:-}" "$BOT_PID" 2>/dev/null || true
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
        'text': '✅ TST Signal Bot ONLINE\nTelegram connected successfully.\nLive market scanner is running.',
        'disable_web_page_preview': True,
    })
    print('[telegram-test] SENT successfully', flush=True)
except Exception as exc:
    print(f'[telegram-test] FAILED {type(exc).__name__}: {exc}', flush=True)
PY
fi

python -u /freqtrade/fast_entry_engine.py &
FAST_PID=$!
echo "[entrypoint] fast entry engine started pid=${FAST_PID}"

if [[ -n "${NEW_LISTING_SYMBOL:-}" && -n "${NEW_LISTING_START_UTC:-}" ]]; then
  python -u /freqtrade/new_listing_watcher.py &
  NEW_LISTING_PID=$!
  echo "[entrypoint] new listing watcher started pid=${NEW_LISTING_PID} symbol=${NEW_LISTING_SYMBOL} start=${NEW_LISTING_START_UTC}"
else
  echo "[entrypoint] new listing watcher disabled: schedule not configured"
fi

wait "$BOT_PID"
exit $?
