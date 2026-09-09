from pathlib import Path

path=Path('/freqtrade/fast_entry_engine.py')
s=path.read_text(encoding='utf-8')

if 'import execution_health\n' not in s:
    marker='import trade_state\n'
    if marker not in s: raise SystemExit('risk-circuit: trade_state import marker missing')
    s=s.replace(marker,marker+'import execution_health\n',1)

const_marker="PORTFOLIO_MAX_POSITIONS = int(os.getenv('FAST_PORTFOLIO_MAX_POSITIONS', '3'))\n"
const_add="""DAILY_REALIZED_LOSS_LIMIT_USDT = float(os.getenv('DAILY_LOSS_LIMIT_USDT', '2.0'))
MAX_CONSECUTIVE_LOSSES = int(os.getenv('FAST_MAX_CONSECUTIVE_LOSSES', '3'))
LOSS_STREAK_COOLDOWN_SEC = int(os.getenv('FAST_LOSS_STREAK_COOLDOWN_SEC', '21600'))
MAX_REALIZED_DRAWDOWN_USDT = float(os.getenv('FAST_MAX_REALIZED_DRAWDOWN_USDT', '4.0'))
DRAWDOWN_COOLDOWN_SEC = int(os.getenv('FAST_DRAWDOWN_COOLDOWN_SEC', '43200'))
EXECUTION_HEALTH_STALE_SEC = int(os.getenv('FAST_EXECUTION_HEALTH_STALE_SEC', '180'))
EXECUTION_HEALTH_MAX_ERRORS = int(os.getenv('FAST_EXECUTION_HEALTH_MAX_ERRORS', '2'))
"""
if 'DAILY_REALIZED_LOSS_LIMIT_USDT' not in s:
    if const_marker not in s: raise SystemExit('risk-circuit: constants marker missing')
    s=s.replace(const_marker,const_marker+const_add,1)

old="""    snap = trade_state.portfolio_snapshot()
    try:
"""
new="""    snap = trade_state.portfolio_snapshot()
    perf = trade_state.performance_snapshot()
    health_ok, health_reason = execution_health.healthy(EXECUTION_HEALTH_STALE_SEC, EXECUTION_HEALTH_MAX_ERRORS)
    if not health_ok:
        return False, health_reason
    realized_today = float(perf.get('realized_pnl_today_usdt') or 0.0)
    if DAILY_REALIZED_LOSS_LIMIT_USDT > 0 and realized_today <= -DAILY_REALIZED_LOSS_LIMIT_USDT:
        return False, f'daily-realized-loss-cap-{realized_today:.3f}<=-{DAILY_REALIZED_LOSS_LIMIT_USDT:.3f}'
    last_closed = float(perf.get('last_closed_at') or 0.0)
    since_close = time.time() - last_closed if last_closed > 0 else 1e18
    streak = int(perf.get('consecutive_losses') or 0)
    if MAX_CONSECUTIVE_LOSSES > 0 and streak >= MAX_CONSECUTIVE_LOSSES and since_close < LOSS_STREAK_COOLDOWN_SEC:
        return False, f'loss-streak-circuit-{streak}-cooldown-{int(LOSS_STREAK_COOLDOWN_SEC-since_close)}s'
    drawdown = float(perf.get('current_realized_drawdown_usdt') or 0.0)
    if MAX_REALIZED_DRAWDOWN_USDT > 0 and drawdown >= MAX_REALIZED_DRAWDOWN_USDT and since_close < DRAWDOWN_COOLDOWN_SEC:
        return False, f'realized-drawdown-circuit-{drawdown:.3f}USDT'
    try:
"""
if 'daily-realized-loss-cap-' not in s:
    if old not in s: raise SystemExit('risk-circuit: portfolio marker missing')
    s=s.replace(old,new,1)

startup_marker="    last_chat_retry = 0.0\n"
startup="""    print(f'[risk-circuits] ONLINE daily_loss={DAILY_REALIZED_LOSS_LIMIT_USDT:.2f}USDT max_streak={MAX_CONSECUTIVE_LOSSES} streak_cooldown={LOSS_STREAK_COOLDOWN_SEC//3600}h drawdown={MAX_REALIZED_DRAWDOWN_USDT:.2f}USDT api_health_stale={EXECUTION_HEALTH_STALE_SEC}s')
"""
if '[risk-circuits] ONLINE' not in s:
    if startup_marker not in s: raise SystemExit('risk-circuit: startup marker missing')
    s=s.replace(startup_marker,startup+startup_marker,1)

compile(s,str(path),'exec'); path.write_text(s,encoding='utf-8')
print('[risk-circuit-patch] OK realized PnL + streak + drawdown + API-health hard gates enabled')
