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

# User-facing final result: every reconciled bot-owned OCO close gets one concise
# Telegram summary. This is notification-only and never changes exchange state.
if 'def _telegram_close_alert(' not in s:
    run_anchor='\ndef run_once():\n'
    if run_anchor not in s: raise SystemExit('reconcile-health: run_once marker missing')
    helper=r'''

def _telegram_close_alert(pos:dict, exit_row:dict) -> None:
    token=(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    chat=(os.getenv('TELEGRAM_CHAT_ID') or '').strip()
    if not token or not chat:
        return
    try:
        symbol=str(pos.get('symbol') or '').upper()
        entry=float(pos.get('entry') or 0.0)
        exit_price=float(exit_row.get('exit_price') or 0.0)
        quote_spent=float(pos.get('quote_spent') or 0.0)
        pnl=float(exit_row.get('realized_pnl_usdt') or 0.0)
        pnl_pct=(pnl/quote_spent*100.0) if quote_spent>0 else 0.0
        reason=str(exit_row.get('close_reason') or 'BINANCE_OCO_EXIT').upper()
        ratchet=str(pos.get('last_live_ratchet_reason') or pos.get('last_ratchet_reason') or '').lower()
        current_stop=float(pos.get('stop') or 0.0)
        if reason=='TAKE_PROFIT':
            label='TAKE PROFIT 🎯'
        elif reason=='STOP_LOSS' and 'red-reversal' in ratchet:
            label='RED REVERSAL PROTECTION 🔒'
        elif reason=='STOP_LOSS' and (('profit' in ratchet) or (entry>0 and current_stop>entry)):
            label='PROFIT LOCK 🔒'
        elif reason=='STOP_LOSS':
            label='STOP LOSS 🛑'
        else:
            label=reason.replace('_',' ')
        icon='✅' if pnl>=0 else '❌'
        text=(
            f'{icon} POSITION CLOSED — {symbol} — SPOT\n'
            f'💵 Size: {quote_spent:.2f} USDT\n'
            f'📍 Entry: {entry:.8g}\n'
            f'🏁 Exit: {exit_price:.8g}\n'
            f'💰 P/L: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\n'
            f'🧾 Reason: {label}'
        )
        data=urllib.parse.urlencode({'chat_id':chat,'text':text,'disable_web_page_preview':'true'}).encode()
        req=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',data=data,method='POST',headers={'User-Agent':'tst-reconciler/1.3'})
        with urllib.request.urlopen(req,timeout=12) as r:
            if r.status>=300:
                raise RuntimeError(f'TELEGRAM_HTTP_{r.status}')
        print(f'[reconcile] Telegram close summary sent {symbol} pnl={pnl:+.4f}USDT',flush=True)
    except Exception as exc:
        print(f'[reconcile] Telegram close summary warning {type(exc).__name__}: {str(exc)[:160]}',flush=True)

'''
    s=s.replace(run_anchor,helper+run_anchor,1)

close_old='''            trade_state.close_position(signal_id,**exit_row,order_list_id=oid)\n            print(f"[reconcile] CLOSED {pos.get('symbol')} reason={exit_row['close_reason']} pnl≈{exit_row['realized_pnl_usdt']:+.4f}USDT list={oid}",flush=True)\n'''
close_new='''            trade_state.close_position(signal_id,**exit_row,order_list_id=oid)\n            _telegram_close_alert(pos,exit_row)\n            print(f"[reconcile] CLOSED {pos.get('symbol')} reason={exit_row['close_reason']} pnl≈{exit_row['realized_pnl_usdt']:+.4f}USDT list={oid}",flush=True)\n'''
if '_telegram_close_alert(pos,exit_row)' not in s:
    if close_old not in s: raise SystemExit('reconcile-health: close marker missing')
    s=s.replace(close_old,close_new,1)

compile(s,str(path),'exec'); path.write_text(s,encoding='utf-8')
print('[reconcile-health-patch] OK persistent API health + Telegram close summaries enabled')
