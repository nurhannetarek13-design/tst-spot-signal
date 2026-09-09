from pathlib import Path

path=Path('/freqtrade/reconcile_state.py')
s=path.read_text(encoding='utf-8')
if 'import execution_health\n' not in s:
    marker='import trade_state\n'
    if marker not in s: raise SystemExit('reconcile-health: import marker missing')
    s=s.replace(marker,marker+'import execution_health\n',1)

old='''    snap=trade_state.portfolio_snapshot(); perf=trade_state.performance_snapshot()\n    print(f"[reconcile] OK bot_open_ocos={len(open_ocos)} tracked_open={snap['open_count']} risk={snap['stop_risk_usdt']:.4f} incomplete={snap['incomplete_count']} pnl_today={perf['realized_pnl_today_usdt']:+.4f} streak={perf['consecutive_losses']}",flush=True)\n'''
new='''    snap=trade_state.portfolio_snapshot(); perf=trade_state.performance_snapshot()\n    execution_health.mark_ok(bot_open_ocos=len(open_ocos),tracked_open=snap['open_count'],incomplete=snap['incomplete_count'])\n    print(f"[reconcile] OK bot_open_ocos={len(open_ocos)} tracked_open={snap['open_count']} risk={snap['stop_risk_usdt']:.4f} incomplete={snap['incomplete_count']} pnl_today={perf['realized_pnl_today_usdt']:+.4f} streak={perf['consecutive_losses']}",flush=True)\n'''
if 'execution_health.mark_ok(' not in s:
    if old not in s: raise SystemExit('reconcile-health: success marker missing')
    s=s.replace(old,new,1)

old_exc='''        except Exception as exc: print(f'[reconcile] loop warning {type(exc).__name__}: {str(exc)[:180]}',flush=True)\n'''
new_exc='''        except Exception as exc:\n            execution_health.mark_error(f'{type(exc).__name__}:{str(exc)[:180]}')\n            print(f'[reconcile] loop warning {type(exc).__name__}: {str(exc)[:180]}',flush=True)\n'''
if 'execution_health.mark_error(' not in s:
    if old_exc not in s: raise SystemExit('reconcile-health: loop marker missing')
    s=s.replace(old_exc,new_exc,1)

compile(s,str(path),'exec'); path.write_text(s,encoding='utf-8')
print('[reconcile-health-patch] OK persistent API health heartbeat enabled')
