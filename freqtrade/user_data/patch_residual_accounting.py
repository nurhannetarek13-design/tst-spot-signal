from pathlib import Path

reconcile_path = Path('/freqtrade/reconcile_state.py')
notifier_path = Path('/freqtrade/user_data/trade_close_notifier.py')

r = reconcile_path.read_text(encoding='utf-8')
n = notifier_path.read_text(encoding='utf-8')

# Binance may charge the BUY commission in base asset and OCO quantities must be
# floored to LOT_SIZE. Keep the trade closed for strategy/risk purposes, but
# persist a conservative estimate of any unsold remainder so it is never shown
# to the user as if it simply disappeared.
residual_anchor = "    closed_ms=int(o.get('updateTime') or o.get('time') or 0)\n    return {\n"
residual_new = """    closed_ms=int(o.get('updateTime') or o.get('time') or 0)
    bought_qty=float(pos.get('quantity') or 0.0)
    residual_qty=max(0.0,bought_qty-qty)
    residual_value=residual_qty*exit_price if exit_price>0 else 0.0
    coverage=(qty/bought_qty*100.0) if bought_qty>0 else 100.0
    return {
"""
if 'residual_qty=max(0.0,bought_qty-qty)' not in r:
    if residual_anchor not in r:
        raise SystemExit('residual-accounting: reconciler return anchor missing')
    r = r.replace(residual_anchor, residual_new, 1)

return_tail = "        'pnl_cost_buffer_pct':PNL_COST_BUFFER_PCT,\n        'closed_at':closed_ms/1000.0 if closed_ms>0 else time.time(),\n"
return_new = "        'pnl_cost_buffer_pct':PNL_COST_BUFFER_PCT,\n        'residual_qty_estimate':residual_qty, 'residual_value_estimate_usdt':residual_value,\n        'exit_quantity_coverage_pct':coverage,\n        'closed_at':closed_ms/1000.0 if closed_ms>0 else time.time(),\n"
if "'residual_qty_estimate':residual_qty" not in r:
    if return_tail not in r:
        raise SystemExit('residual-accounting: reconciler payload anchor missing')
    r = r.replace(return_tail, return_new, 1)

# User-facing close message: explicitly separate sold value from an estimated
# LOT_SIZE/fee residual. This fixes the confusing Stake 6.69 / Exit value 5.69
# presentation seen on CRCLB.
var_anchor = "    exit_quote = float(pos.get('exit_quote') or 0.0)\n    pnl = float(pos.get('realized_pnl_usdt') or 0.0)\n"
var_new = """    exit_quote = float(pos.get('exit_quote') or 0.0)
    residual_qty = float(pos.get('residual_qty_estimate') or 0.0)
    residual_value = float(pos.get('residual_value_estimate_usdt') or 0.0)
    coverage = float(pos.get('exit_quantity_coverage_pct') or 100.0)
    pnl = float(pos.get('realized_pnl_usdt') or 0.0)
"""
if 'residual_qty = float(pos.get(' not in n:
    if var_anchor not in n:
        raise SystemExit('residual-accounting: notifier variable anchor missing')
    n = n.replace(var_anchor, var_new, 1)

text_anchor = """    text = (
        f'{result_emoji} TRADE CLOSED — {symbol} — SPOT\\n'
        f'💵 Stake {quote_spent:.2f} USDT\\n'
        f'💲 Entry {entry:.8g}\\n'
        f'🏁 Exit {exit_price:.8g}\\n'
        f'📊 Result {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\\n'
        f'📌 Reason {reason_label}\\n'
        f'⏱ Held {held_min} min\\n'
        f'💰 Exit value {exit_quote:.2f} USDT'
    )
"""
text_new = """    residual_line = ''
    if residual_qty > 1e-12 and residual_value >= 0.01:
        residual_line = (
            f'\\n🪙 Residual est. {residual_qty:.8g} {symbol[:-4]} '
            f'(≈{residual_value:.2f} USDT, exchange fee/LOT_SIZE; not counted as cash exit)'
        )
    text = (
        f'{result_emoji} TRADE CLOSED — {symbol} — SPOT\\n'
        f'💵 Stake {quote_spent:.2f} USDT\\n'
        f'💲 Entry {entry:.8g}\\n'
        f'🏁 Exit {exit_price:.8g}\\n'
        f'📊 Realized {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\\n'
        f'📌 Reason {reason_label}\\n'
        f'⏱ Held {held_min} min\\n'
        f'💰 Sold value {exit_quote:.2f} USDT'
        f'{residual_line}'
    )
"""
if 'Residual est.' not in n:
    if text_anchor not in n:
        raise SystemExit('residual-accounting: notifier text anchor missing')
    n = n.replace(text_anchor, text_new, 1)

for marker in ['residual_qty_estimate', 'residual_value_estimate_usdt', 'exit_quantity_coverage_pct']:
    if marker not in r:
        raise SystemExit(f'residual-accounting: missing reconciler marker {marker}')
for marker in ['Residual est.', 'Sold value', "exchange fee/LOT_SIZE"]:
    if marker not in n:
        raise SystemExit(f'residual-accounting: missing notifier marker {marker}')

compile(r, str(reconcile_path), 'exec')
compile(n, str(notifier_path), 'exec')
reconcile_path.write_text(r, encoding='utf-8')
notifier_path.write_text(n, encoding='utf-8')
print('[residual-accounting] OK OCO residual estimate persisted + Telegram cash-vs-residual disclosure enabled')
