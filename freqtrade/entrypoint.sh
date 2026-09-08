#!/usr/bin/env bash
set -euo pipefail

/freqtrade/run_ready_bot.sh &
BOT_PID=$!

cleanup() {
  kill "${FAST_PID:-}" "$BOT_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

# The bridge is downloaded by run_ready_bot.sh. Wait for it before starting
# the independent fast signal engine so imports are deterministic.
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

# Give the bridge a moment to bind port 8080 / resolve Telegram chat.
sleep 3
python -u /freqtrade/fast_entry_engine.py &
FAST_PID=$!
echo "[entrypoint] fast entry engine started pid=${FAST_PID}"

# Main service lifetime follows the existing NFI runtime. If the fast engine
# exits unexpectedly, log it but do not take the signal service down.
(
  wait "$FAST_PID"
  code=$?
  echo "[entrypoint] fast entry engine exited code=${code}" >&2
) &

wait "$BOT_PID"
exit $?
