from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'import execution_health\n' not in s:
    import_anchor='import trade_state\n'
    if import_anchor not in s:
        raise SystemExit('expert-v6: trade_state import marker missing')
    s=s.replace(import_anchor,import_anchor+'import execution_health\n',1)

const_anchor = "PORTFOLIO_MAX_POSITIONS = int(os.getenv('FAST_PORTFOLIO_MAX_POSITIONS', '3'))\n"
const_add = """PORTFOLIO_MAX_POSITIONS = int(os.getenv('FAST_PORTFOLIO_MAX_POSITIONS', '3'))
EXPERT_DAILY_LOSS_CAP_USDT = max(0.1, float(os.getenv('DAILY_LOSS_LIMIT_USDT', '2.0')))
EXPERT_LOSS_STREAK_LIMIT = max(2, int(os.getenv('FAST_LOSS_STREAK_LIMIT', '3')))
EXPERT_LOSS_STREAK_COOLOFF_SEC = max(1800, int(os.getenv('FAST_LOSS_STREAK_COOLOFF_SEC', '21600')))
EXPERT_MAX_REALIZED_DRAWDOWN_USDT = max(0.1, float(os.getenv('FAST_MAX_REALIZED_DRAWDOWN_USDT', '4.0')))
EXPERT_DRAWDOWN_COOLOFF_SEC = max(3600, int(os.getenv('FAST_DRAWDOWN_COOLOFF_SEC', '43200')))
EXPERT_EXEC_HEALTH_STALE_SEC = max(60, int(os.getenv('FAST_EXECUTION_HEALTH_STALE_SEC', '180')))
EXPERT_EXEC_HEALTH_MAX_ERRORS = max(1, int(os.getenv('FAST_EXECUTION_HEALTH_MAX_ERRORS', '2')))
"""
if 'EXPERT_DAILY_LOSS_CAP_USDT' not in s:
    if const_anchor not in s:
        raise SystemExit('expert-v6: risk constants marker missing')
    s = s.replace(const_anchor, const_add, 1)

portfolio_anchor = """    snap = trade_state.portfolio_snapshot()
    try:
        entry = float(payload.get('entry') or 0)
"""
portfolio_new = """    health_ok, health_reason = execution_health.healthy(EXPERT_EXEC_HEALTH_STALE_SEC, EXPERT_EXEC_HEALTH_MAX_ERRORS)
    if not health_ok:
        return False, health_reason

    perf = trade_state.performance_snapshot()
    realized_today = float(perf.get('realized_pnl_today_usdt') or 0.0)
    if realized_today <= -EXPERT_DAILY_LOSS_CAP_USDT + 1e-9:
        return False, f'daily-realized-loss-cap-{realized_today:.3f}<=-{EXPERT_DAILY_LOSS_CAP_USDT:.3f}'
    streak = int(perf.get('consecutive_losses') or 0)
    last_closed = float(perf.get('last_closed_at') or 0.0)
    since_close = time.time() - last_closed if last_closed > 0 else 1e18
    if streak >= EXPERT_LOSS_STREAK_LIMIT and last_closed > 0 and since_close < EXPERT_LOSS_STREAK_COOLOFF_SEC:
        remain = int(EXPERT_LOSS_STREAK_COOLOFF_SEC - since_close)
        return False, f'loss-streak-cooloff-{streak}-remain-{max(0,remain)}s'
    drawdown = float(perf.get('current_realized_drawdown_usdt') or 0.0)
    if drawdown >= EXPERT_MAX_REALIZED_DRAWDOWN_USDT and last_closed > 0 and since_close < EXPERT_DRAWDOWN_COOLOFF_SEC:
        remain = int(EXPERT_DRAWDOWN_COOLOFF_SEC - since_close)
        return False, f'realized-drawdown-cooloff-{drawdown:.3f}USDT-remain-{max(0,remain)}s'

    snap = trade_state.portfolio_snapshot()
    try:
        entry = float(payload.get('entry') or 0)
"""
if 'daily-realized-loss-cap-' not in s:
    if portfolio_anchor not in s:
        raise SystemExit('expert-v6: portfolio gate marker missing')
    s = s.replace(portfolio_anchor, portfolio_new, 1)

startup_anchor = "    print('[expert-system-v5] ONLINE regime=SHADOW cross_sectional_RS=SHADOW live_gate=UNCHANGED research_context=PERSISTENT')\n"
startup_add = startup_anchor + "    print(f'[expert-system-v6] ONLINE daily_loss={EXPERT_DAILY_LOSS_CAP_USDT:.2f}USDT loss_streak={EXPERT_LOSS_STREAK_LIMIT}/{EXPERT_LOSS_STREAK_COOLOFF_SEC//3600}h drawdown={EXPERT_MAX_REALIZED_DRAWDOWN_USDT:.2f}USDT/{EXPERT_DRAWDOWN_COOLOFF_SEC//3600}h api_health={EXPERT_EXEC_HEALTH_STALE_SEC}s source=BINANCE_RECONCILED_PNL')\n"
if '[expert-system-v6] ONLINE' not in s:
    if startup_anchor not in s:
        raise SystemExit('expert-v6: startup marker missing')
    s = s.replace(startup_anchor, startup_add, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[expert-system-v6-patch] OK reconciled PnL daily/streak/drawdown + persistent Binance API-health hard gates enabled')
