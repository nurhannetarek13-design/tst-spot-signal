from pathlib import Path

entrypoint = Path('/freqtrade/entrypoint.sh')
hard_exit = Path('/freqtrade/user_data/hard_time_exit_manager.py')
notifier = Path('/freqtrade/user_data/trade_close_notifier.py')

for p in (hard_exit, notifier):
    if not p.exists():
        raise SystemExit(f'hard-time-exit lifecycle: missing {p}')
    compile(p.read_text(encoding='utf-8'), str(p), 'exec')

s = entrypoint.read_text(encoding='utf-8')

if '[entrypoint] hard 60m ownership-safe exit manager started' not in s:
    cleanup_old = '  kill "${FRONT_PROXY_PID:-}" "${EV_VALIDATION_PID:-}" "${SHADOW_EV_PID:-}" "${SHADOW_RESEARCH_PID:-}" "${MARKET_CONTEXT_PID:-}" "${RECOVERY_PID:-}" "${RECONCILE_PID:-}" "${DYNAMIC_EXIT_PID:-}" "${OUTCOME_ENGINE_PID:-}" "${SOL_MONITOR_PID:-}" "${NEW_LISTING_PID:-}" "${FAST_PID:-}" "$BOT_PID" 2>/dev/null || true\n'
    cleanup_new = '  kill "${FRONT_PROXY_PID:-}" "${EV_VALIDATION_PID:-}" "${SHADOW_EV_PID:-}" "${SHADOW_RESEARCH_PID:-}" "${MARKET_CONTEXT_PID:-}" "${RECOVERY_PID:-}" "${RECONCILE_PID:-}" "${DYNAMIC_EXIT_PID:-}" "${HARD_TIME_EXIT_PID:-}" "${CLOSE_NOTIFIER_PID:-}" "${OUTCOME_ENGINE_PID:-}" "${SOL_MONITOR_PID:-}" "${NEW_LISTING_PID:-}" "${FAST_PID:-}" "$BOT_PID" 2>/dev/null || true\n'
    if cleanup_old not in s:
        raise SystemExit('hard-time-exit lifecycle: cleanup anchor missing')
    s = s.replace(cleanup_old, cleanup_new, 1)

    start_anchor = '''python -u /freqtrade/dynamic_exit_manager.py &
DYNAMIC_EXIT_PID=$!
echo "[entrypoint] ownership-safe dynamic exit manager started pid=${DYNAMIC_EXIT_PID}"
'''
    start_block = start_anchor + '''
python -u /freqtrade/user_data/hard_time_exit_manager.py &
HARD_TIME_EXIT_PID=$!
echo "[entrypoint] hard 60m ownership-safe exit manager started pid=${HARD_TIME_EXIT_PID}"

python -u /freqtrade/user_data/trade_close_notifier.py &
CLOSE_NOTIFIER_PID=$!
echo "[entrypoint] final trade-result Telegram notifier started pid=${CLOSE_NOTIFIER_PID}"
'''
    if start_anchor not in s:
        raise SystemExit('hard-time-exit lifecycle: dynamic-exit start anchor missing')
    s = s.replace(start_anchor, start_block, 1)

    supervisor_anchor = '''  if ! kill -0 "$RECONCILE_PID" 2>/dev/null; then
    echo "[entrypoint] CRITICAL reconciler exited; fail-closed restart" >&2
    wait "$RECONCILE_PID" || true
    exit 1
  fi
'''
    supervisor_block = supervisor_anchor + '''  if ! kill -0 "$HARD_TIME_EXIT_PID" 2>/dev/null; then
    echo "[entrypoint] CRITICAL hard-time-exit manager exited; fail-closed restart" >&2
    wait "$HARD_TIME_EXIT_PID" || true
    exit 1
  fi
  if ! kill -0 "$CLOSE_NOTIFIER_PID" 2>/dev/null; then
    echo "[entrypoint] close notifier exited; restarting container to restore lifecycle reporting" >&2
    wait "$CLOSE_NOTIFIER_PID" || true
    exit 1
  fi
'''
    if supervisor_anchor not in s:
        raise SystemExit('hard-time-exit lifecycle: supervisor anchor missing')
    s = s.replace(supervisor_anchor, supervisor_block, 1)

for required in [
    'hard_time_exit_manager.py',
    'trade_close_notifier.py',
    'HARD_TIME_EXIT_PID',
    'CLOSE_NOTIFIER_PID',
    'hard 60m ownership-safe exit manager started',
    'CRITICAL hard-time-exit manager exited',
]:
    if required not in s:
        raise SystemExit(f'hard-time-exit lifecycle: missing {required}')

entrypoint.write_text(s, encoding='utf-8')
print('[hard-time-exit-lifecycle-patch] OK 60m hard exit + final Telegram PnL notifier wired into supervised runtime')
