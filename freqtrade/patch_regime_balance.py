from pathlib import Path

p = Path('/freqtrade/spot_sniper_gate.py')
g = p.read_text(encoding='utf-8')

# The live market-context collector is intentionally heuristic/shadow telemetry.
# It may produce a small point-in-time sample when public kline requests are only
# partially successful. Therefore WEAK_BEAR must not be an unconditional veto on
# an otherwise fully-confirmed symbol-specific setup. PANIC remains a hard block.
#
# Policy:
# - WEAK_BEAR exception is limited to MID continuation and EARLY REVERSAL only.
# - MID needs score >=97; REVERSAL needs score >=98 after lane penalty.
# - NORMAL / EXPLOSIVE / EXTREME stay blocked in WEAK_BEAR.
# - With a reliable context sample (>=40 symbols), rank is still enforced and is
#   tighter in WEAK_BEAR (top 10).
# - With a thin context sample, rank is advisory; an unranked symbol needs 100.
# - All upstream BTC/HTF/microstructure/persistence and downstream portfolio/OCO
#   risk gates remain unchanged.

const_marker = "FALLBACK_HIGH_SCORE_MAX_RANK = max(FALLBACK_MAX_RANK, int(os.getenv('SPOT_SNIPER_HIGH_SCORE_MAX_RANK', '15')))\n"
const_extra = (
    "FALLBACK_WEAK_BEAR_BASE_SCORE = float(os.getenv('SPOT_SNIPER_WEAK_BEAR_BASE_SCORE', '95'))\n"
    "FALLBACK_WEAK_BEAR_MAX_RANK = max(1, int(os.getenv('SPOT_SNIPER_WEAK_BEAR_MAX_RANK', '10')))\n"
    "FALLBACK_MIN_RELIABLE_RANK_UNIVERSE = max(20, int(os.getenv('SPOT_SNIPER_MIN_RELIABLE_RANK_UNIVERSE', '40')))\n"
    "FALLBACK_THIN_CONTEXT_UNRANKED_SCORE = float(os.getenv('SPOT_SNIPER_THIN_CONTEXT_UNRANKED_SCORE', '100'))\n"
)
if 'FALLBACK_WEAK_BEAR_BASE_SCORE =' not in g:
    if const_marker not in g:
        raise SystemExit('regime-balance: constants marker missing')
    g = g.replace(const_marker, const_marker + const_extra, 1)

old_threshold = """def _fallback_threshold(regime: str, lane: str) -> float | None:
    if regime == 'STRONG_BULL':
        base = FALLBACK_STRONG_BULL_SCORE
    elif regime == 'WEAK_BULL':
        base = FALLBACK_WEAK_BULL_SCORE
    elif regime == 'POST_CRASH_RECOVERY':
        base = FALLBACK_POST_CRASH_SCORE
    elif regime == 'SIDEWAYS_COMPRESSION':
        base = FALLBACK_SIDEWAYS_SCORE
    else:
        return None

    penalty = {
"""
new_threshold = """def _fallback_threshold(regime: str, lane: str) -> float | None:
    if regime == 'STRONG_BULL':
        base = FALLBACK_STRONG_BULL_SCORE
    elif regime == 'WEAK_BULL':
        base = FALLBACK_WEAK_BULL_SCORE
    elif regime == 'POST_CRASH_RECOVERY':
        base = FALLBACK_POST_CRASH_SCORE
    elif regime == 'SIDEWAYS_COMPRESSION':
        base = FALLBACK_SIDEWAYS_SCORE
    elif regime == 'WEAK_BEAR':
        # Only symbol-specific relative-strength/reversal lanes may override the
        # broad weak tape, and only at exceptional lane scores.
        if lane not in {'MID', 'REVERSAL'}:
            return None
        base = FALLBACK_WEAK_BEAR_BASE_SCORE
    else:
        return None

    penalty = {
"""
if new_threshold not in g:
    if old_threshold not in g:
        raise SystemExit('regime-balance: fallback threshold marker missing')
    g = g.replace(old_threshold, new_threshold, 1)

old_rank = """    effective_max_rank = FALLBACK_HIGH_SCORE_MAX_RANK if score >= 98.0 else FALLBACK_MAX_RANK
    if rank_i is None or rank_i > effective_max_rank:
        return _reject(
            'FALLBACK_RANK_REJECT', f'production-fallback-rank>{effective_max_rank}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
        )

"""
new_rank = """    try:
        context_n = int(ctx.get('universe_n') or 0)
    except Exception:
        context_n = 0
    rank_reliable = context_n >= FALLBACK_MIN_RELIABLE_RANK_UNIVERSE
    effective_max_rank = FALLBACK_HIGH_SCORE_MAX_RANK if score >= 98.0 else FALLBACK_MAX_RANK
    if regime == 'WEAK_BEAR':
        effective_max_rank = min(effective_max_rank, FALLBACK_WEAK_BEAR_MAX_RANK)

    if rank_reliable and (rank_i is None or rank_i > effective_max_rank):
        return _reject(
            'FALLBACK_RANK_REJECT', f'production-fallback-rank>{effective_max_rank}',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
            context_universe_n=context_n, rank_reliable=True,
        )

    # A thin snapshot must not become a global kill-switch. If the candidate was
    # not ranked at all, require a perfect lane score before allowing the normal
    # downstream safety gates to decide.
    if not rank_reliable and rank_i is None and score < FALLBACK_THIN_CONTEXT_UNRANKED_SCORE:
        return _reject(
            'FALLBACK_RANK_REJECT', 'thin-context-unranked-needs-score100',
            regime=regime, age=age, rank=rank_i, ev=ev, lane=lane,
            fallback_score_required=required_score, fallback_max_rank=effective_max_rank,
            context_universe_n=context_n, rank_reliable=False,
        )

"""
if new_rank not in g:
    if old_rank not in g:
        raise SystemExit('regime-balance: rank gate marker missing')
    g = g.replace(old_rank, new_rank, 1)

for marker in [
    'FALLBACK_WEAK_BEAR_BASE_SCORE =',
    "regime == 'WEAK_BEAR'",
    "lane not in {'MID', 'REVERSAL'}",
    'FALLBACK_MIN_RELIABLE_RANK_UNIVERSE',
    'thin-context-unranked-needs-score100',
]:
    if marker not in g:
        raise SystemExit(f'regime-balance: missing marker {marker}')

compile(g, str(p), 'exec')
p.write_text(g, encoding='utf-8')
print('[regime-balance] OK weak-bear is selective not absolute; MID>=97 REVERSAL>=98; panic unchanged; thin-context rank cannot deadlock score100')

# ---------------------------------------------------------------------------
# Fee-safe ownership-aware OCO protection hardening.
# XLM exposed a concrete failure mode: Binance charged the BUY commission in the
# base asset, so executedQty was larger than the quantity actually available for
# the protective SELL. The old code tried to OCO the gross quantity and Binance
# rejected it as insufficient balance. This patch makes the bot track the net
# bot-owned quantity, caps every OCO to that quantity, and safely recovers a
# terminal failed protection attempt without touching manual/Copy holdings.
# ---------------------------------------------------------------------------

def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise SystemExit(f'fee-safe-oco: marker missing: {label}')
    return text.replace(old, new, 1)

# 1) Read-only Binance helpers: exact base-commission accounting, regular Spot
# free balance, and external/manual SELL detection.
rp = Path('/freqtrade/reconcile_state.py')
r = rp.read_text(encoding='utf-8')
helper_marker = "\n\ndef _order(symbol,order_id):\n"
helper_code = r'''

BASE_FEE_FALLBACK_PCT=max(0.0015,float(os.getenv('OCO_BASE_FEE_FALLBACK_PCT','0.0020')))

def spot_free_balance(asset:str)->float:
    asset=str(asset or '').upper().strip()
    if not asset: return 0.0
    account=_signed_get('/api/v3/account',{})
    for row in (account.get('balances') or []) if isinstance(account,dict) else []:
        if str(row.get('asset') or '').upper()==asset:
            try: return max(0.0,float(row.get('free') or 0))
            except Exception: return 0.0
    return 0.0


def buy_sellable_quantity(symbol:str,order_id,gross_qty)->dict:
    """Return bot-owned SELLable qty after base-asset BUY commission.

    Primary source is Binance myTrades. If that read is temporarily unavailable,
    reserve 0.20% of gross qty so protection never borrows manual holdings.
    """
    symbol=str(symbol or '').upper(); base=symbol[:-4] if symbol.endswith('USDT') else ''
    try: gross=max(0.0,float(gross_qty or 0))
    except Exception: gross=0.0
    fallback=max(0.0,gross*(1.0-BASE_FEE_FALLBACK_PCT))
    try:
        oid=int(float(order_id or 0))
        if oid<=0 or not base: raise RuntimeError('missing-order-id')
        rows=_signed_get('/api/v3/myTrades',{'symbol':symbol,'orderId':oid,'limit':100})
        matched=[x for x in rows if isinstance(x,dict) and int(x.get('orderId') or 0)==oid and bool(x.get('isBuyer'))] if isinstance(rows,list) else []
        if not matched: raise RuntimeError('buy-fills-not-visible-yet')
        commission=sum(float(x.get('commission') or 0) for x in matched if str(x.get('commissionAsset') or '').upper()==base)
        sellable=max(0.0,gross-commission)
        return {'gross_quantity':gross,'sellable_quantity':sellable,'base_commission':commission,'source':'BINANCE_MYTRADES'}
    except Exception as exc:
        return {'gross_quantity':gross,'sellable_quantity':fallback,'base_commission':max(0.0,gross-fallback),'source':f'FEE_RESERVE_FALLBACK:{type(exc).__name__}'}


def sell_qty_since(symbol:str,since_epoch:float)->float:
    """Any Spot SELL after this bot position opened means ownership is ambiguous.

    Recovery then stops managing that position rather than risk protecting or
    selling manually-owned coins.
    """
    try: start_ms=max(0,int(float(since_epoch or 0)*1000)-1000)
    except Exception: start_ms=0
    if start_ms<=0: return 0.0
    rows=_signed_get('/api/v3/myTrades',{'symbol':str(symbol or '').upper(),'startTime':start_ms,'limit':1000})
    total=0.0
    for row in rows if isinstance(rows,list) else []:
        try:
            if not bool(row.get('isBuyer')): total+=float(row.get('qty') or 0)
        except Exception: pass
    return max(0.0,total)
'''
if 'def buy_sellable_quantity(' not in r:
    if helper_marker not in r:
        raise SystemExit('fee-safe-oco: reconcile helper marker missing')
    r = r.replace(helper_marker, helper_code + helper_marker, 1)
compile(r, str(rp), 'exec')
rp.write_text(r, encoding='utf-8')

# 2) Canonical state stores NET bot-owned quantity, not gross exchange fill.
tp = Path('/freqtrade/trade_state.py')
t = tp.read_text(encoding='utf-8')
t = _replace_once(
    t,
    "            'quantity': body.get('quantity'),\n            'take_profit_price': body.get('take_profit_price'),\n",
    "            'quantity': body.get('quantity'),\n            'protection_attempt': body.get('protection_attempt'),\n            'take_profit_price': body.get('take_profit_price'),\n",
    'reservation protection attempt',
)
old_buy = '''    try:
        qty = float(response.get('executed_qty') or 0)
        quote = float(response.get('quote_spent') or 0)
    except Exception:
        qty = quote = 0.0
    if qty <= 0 or quote <= 0:
        return
    entry = quote / qty
'''
new_buy = '''    try:
        gross_qty = float(response.get('gross_executed_qty') or response.get('executed_qty') or 0)
        qty = float(response.get('sellable_qty') or gross_qty)
        quote = float(response.get('quote_spent') or 0)
    except Exception:
        gross_qty = qty = quote = 0.0
    if gross_qty <= 0 or qty <= 0 or quote <= 0:
        return
    # Cost per sellable unit includes a base-asset commission when one was paid.
    entry = quote / qty
'''
t = _replace_once(t, old_buy, new_buy, 'record_buy net quantity')
t = _replace_once(
    t,
    "            'quantity': qty,\n            'quote_spent': quote,\n            'buy_order_id': response.get('order_id'),\n",
    "            'quantity': qty,\n            'gross_quantity': gross_qty,\n            'base_commission': float(response.get('base_commission') or max(0.0, gross_qty-qty)),\n            'quantity_fee_adjusted': bool(response.get('sellable_qty') is not None),\n            'quantity_source': response.get('sellable_qty_source') or 'LEGACY_GROSS',\n            'quote_spent': quote,\n            'buy_order_id': response.get('order_id'),\n",
    'record_buy metadata',
)
reservation_marker = "\n\ndef record_buy(body: dict[str, Any], response: dict[str, Any]) -> None:\n"
reservation_helpers = r'''

def release_terminal_reservation(signal_id:str, action:str, allowed: set[str] | None=None) -> bool:
    """Release only a reconciled terminal failure so protection may retry safely."""
    key=f'{signal_id}:{action.upper()}'
    allowed={x.upper() for x in (allowed or {'NOT_FOUND','REJECTED','CLOSED_NO_FILL'})}
    with _LOCK:
        state=load_reservations(); row=state.get('reservations',{}).get(key)
        if not isinstance(row,dict) or str(row.get('status') or '').upper() not in allowed:
            return False
        previous=str(row.get('status') or '').upper()
        del state['reservations'][key]; save_reservations(state)
        append_event('TERMINAL_RESERVATION_RELEASED',signal_id=signal_id,action=action.upper(),previous_status=previous)
        return True

'''
if 'def release_terminal_reservation(' not in t:
    if reservation_marker not in t:
        raise SystemExit('fee-safe-oco: reservation helper marker missing')
    t=t.replace(reservation_marker,reservation_helpers+reservation_marker,1)
external_marker = "\n\ndef close_position(signal_id: str, *, exit_price: float, exit_qty: float, exit_quote: float,\n"
external_helper = r'''

def mark_externally_closed(signal_id:str, reason:str='EXTERNAL_INTERVENTION', **extra:Any) -> None:
    """Stop bot ownership after a manual/external asset change; never fabricate PnL."""
    with _LOCK:
        state=load_state(); pos=state.get('positions',{}).get(signal_id)
        if not isinstance(pos,dict): return
        pos.update({'status':'EXTERNALLY_CLOSED','closed_at':time.time(),'close_reason':reason,'updated_at':time.time(),**extra})
        state['positions'][signal_id]=pos; save_state(state)
        append_event('POSITION_EXTERNALLY_CLOSED',signal_id=signal_id,symbol=pos.get('symbol'),reason=reason,**extra)

'''
if 'def mark_externally_closed(' not in t:
    if external_marker not in t:
        raise SystemExit('fee-safe-oco: external close marker missing')
    t=t.replace(external_marker,external_helper+external_marker,1)
compile(t, str(tp), 'exec')
tp.write_text(t, encoding='utf-8')

# 3) Relay enriches BUY fills with exact net quantity and caps every OCO to the
# tracked bot-owned quantity. A regular Spot balance read may only reduce it.
fp = Path('/freqtrade/front_proxy.py')
f = fp.read_text(encoding='utf-8')
if 'import reconcile_state\n' not in f:
    f = _replace_once(f, 'import binance_filters\nimport trade_state\n', 'import binance_filters\nimport reconcile_state\nimport trade_state\n', 'front proxy reconcile import')
old_oco = '''                pos=trade_state.position_for_signal(signal_id)
                if not pos or str(pos.get('status') or '') not in {'BUY_FILLED','PROTECTION_PENDING','OCO_ACTIVE'}:
                    return self.send_json(409,{'ok':False,'status':'UNKNOWN_BUY_FOR_OCO','signal_id':signal_id})
                normalized,filter_meta=binance_filters.normalize_oco(symbol,quantity,tp,sl,sl_limit)
                body.update(normalized)
                body['execution_filter_meta']=filter_meta
                body['list_client_order_id']=_client_id('TSTO-',signal_id)
                body['stop_client_order_id']=_client_id('TSTS-',signal_id)
                body['limit_client_order_id']=_client_id('TSTL-',signal_id)
                target_url=MAKE_OCO_WEBHOOK_URL
'''
new_oco = '''                pos=trade_state.position_for_signal(signal_id)
                if not pos or str(pos.get('status') or '') not in {'BUY_FILLED','PROTECTION_PENDING','OCO_ACTIVE'}:
                    return self.send_json(409,{'ok':False,'status':'UNKNOWN_BUY_FOR_OCO','signal_id':signal_id})
                tracked_qty=float(pos.get('quantity') or 0)
                if tracked_qty<=0:
                    return self.send_json(409,{'ok':False,'status':'NO_TRACKED_SELLABLE_QTY','signal_id':signal_id})
                requested_qty=quantity
                # Ownership cap is authoritative. Wallet balance is allowed to
                # reduce this amount, never increase it into manual holdings.
                quantity=min(quantity,tracked_qty)
                try:
                    free_qty=reconcile_state.spot_free_balance(symbol[:-4])
                    quantity=min(quantity,free_qty)
                    body['spot_free_qty_at_protection']=free_qty
                except Exception as exc:
                    body['spot_free_qty_check']=f'UNAVAILABLE:{type(exc).__name__}'
                normalized,filter_meta=binance_filters.normalize_oco(symbol,quantity,tp,sl,sl_limit)
                body.update(normalized)
                body['requested_quantity']=requested_qty
                body['tracked_quantity_cap']=tracked_qty
                body['execution_filter_meta']=filter_meta
                try: protection_attempt=max(0,int(body.get('protection_attempt') or 0))
                except Exception: protection_attempt=0
                protection_key=signal_id if protection_attempt<=0 else f'{signal_id}:P{protection_attempt}'
                body['protection_attempt']=protection_attempt
                body['list_client_order_id']=_client_id('TSTO-',protection_key)
                body['stop_client_order_id']=_client_id('TSTS-',protection_key)
                body['limit_client_order_id']=_client_id('TSTL-',protection_key)
                target_url=MAKE_OCO_WEBHOOK_URL
'''
f = _replace_once(f, old_oco, new_oco, 'OCO ownership cap')
old_buy_success = '''                if action == 'BUY' and str(row.get('status') or '') == 'BUY_FILLED':
                    trade_state.record_buy(body, row)
                    trade_state.update_position(signal_id,status='PROTECTION_PENDING')
                    print(f"[trade-state] BUY tracked signal={row.get('signal_id') or signal_id} symbol={symbol} protection=PENDING", flush=True)
'''
new_buy_success = '''                if action == 'BUY' and str(row.get('status') or '') == 'BUY_FILLED':
                    fee_meta=reconcile_state.buy_sellable_quantity(symbol,row.get('order_id'),row.get('executed_qty'))
                    row['gross_executed_qty']=fee_meta['gross_quantity']
                    row['sellable_qty']=fee_meta['sellable_quantity']
                    row['base_commission']=fee_meta['base_commission']
                    row['sellable_qty_source']=fee_meta['source']
                    trade_state.record_buy(body, row)
                    trade_state.update_position(signal_id,status='PROTECTION_PENDING')
                    print(f"[trade-state] BUY tracked signal={row.get('signal_id') or signal_id} symbol={symbol} sellable={fee_meta['sellable_quantity']:.10g}/{fee_meta['gross_quantity']:.10g} source={fee_meta['source']} protection=PENDING", flush=True)
'''
f = _replace_once(f, old_buy_success, new_buy_success, 'BUY commission enrichment')
compile(f, str(fp), 'exec')
fp.write_text(f, encoding='utf-8')

# 4) Recovery may retry only after Binance proves the old protection attempt is
# terminal/not-found. Manual/external SELL activity clears stale bot ownership
# instead of blocking all future entries or touching ambiguous holdings.
epp = Path('/freqtrade/execution_recovery.py')
e = epp.read_text(encoding='utf-8')
e = _replace_once(
    e,
    "        'quantity':row.get('quantity'),\n    }\n",
    "        'quantity':row.get('quantity'),\n        'protection_attempt':row.get('protection_attempt'),\n    }\n",
    'recovery reservation body attempt',
)
old_resolve_buy = '''        resp={'ok':True,'status':'BUY_FILLED','signal_id':signal_id,'symbol':symbol,
              'order_id':order.get('orderId'),'client_order_id':client_id,
              'executed_qty':qty,'quote_spent':quote,'recovered':True}
        trade_state.record_buy(body,resp)
'''
new_resolve_buy = '''        resp={'ok':True,'status':'BUY_FILLED','signal_id':signal_id,'symbol':symbol,
              'order_id':order.get('orderId'),'client_order_id':client_id,
              'executed_qty':qty,'quote_spent':quote,'recovered':True}
        fee_meta=reconcile_state.buy_sellable_quantity(symbol,order.get('orderId'),qty)
        resp.update({'gross_executed_qty':fee_meta['gross_quantity'],'sellable_qty':fee_meta['sellable_quantity'],
                     'base_commission':fee_meta['base_commission'],'sellable_qty_source':fee_meta['source']})
        trade_state.record_buy(body,resp)
'''
e = _replace_once(e, old_resolve_buy, new_resolve_buy, 'recovered BUY commission enrichment')
old_recover = '''def _recover_protection(pos:dict)->None:
    if not AUTO_PROTECT or str(pos.get('status') or '')!='PROTECTION_PENDING': return
    signal_id=str(pos.get('signal_id') or ''); symbol=str(pos.get('symbol') or '')
    if not signal_id or not symbol: return
    existing=trade_state.get_reservation(signal_id,'OCO')
    if existing:
        return  # resolver handles uncertain/placed OCO; never blind-resubmit
    try:
        qty=float(pos.get('quantity') or 0); tp=float(pos.get('target') or 0); sl=float(pos.get('stop') or 0)
    except Exception: qty=tp=sl=0
    if qty<=0 or tp<=sl or sl<=0:
        _alert(f'🚨 {symbol} BUY confirmed but protection intent is incomplete. New entries stay blocked.')
        return
    # Conservative stop-limit offset; Binance filter normalizer snaps it to tick size.
    sl_limit=sl*0.997
    body={'signal_id':signal_id,'action':'OCO','symbol':symbol,'quantity':qty,
          'take_profit_price':tp,'model_take_profit_price':pos.get('model_target'),
          'stop_loss_price':sl,'stop_limit_price':sl_limit,
          'confirmed':True,'dry_run':False,'timestamp':int(time.time())}
    try:
        code,row=_signed_local_oco(body)
        if code<300 and row.get('ok') is True:
            print(f'[execution-recovery] protection restored {symbol} signal={signal_id}',flush=True)
        else:
            _alert(f'🚨 Protection recovery failed for {symbol}: {row.get("status") or code}. New entries remain blocked.')
    except Exception as exc:
        _alert(f'🚨 Protection recovery transport failure for {symbol}: {type(exc).__name__}. New entries remain blocked.')
'''
new_recover = '''def _recover_protection(pos:dict)->None:
    if not AUTO_PROTECT or str(pos.get('status') or '')!='PROTECTION_PENDING': return
    signal_id=str(pos.get('signal_id') or ''); symbol=str(pos.get('symbol') or '')
    if not signal_id or not symbol: return

    existing=trade_state.get_reservation(signal_id,'OCO')
    if existing:
        estatus=str(existing.get('status') or '').upper()
        if estatus in {'RESERVED','UNKNOWN','PLACED'}:
            return  # exact client-id resolver must decide first; never blind-resubmit
        if estatus in {'NOT_FOUND','REJECTED','CLOSED_NO_FILL'}:
            if not trade_state.release_terminal_reservation(signal_id,'OCO'):
                return
        else:
            return

    try:
        qty=float(pos.get('quantity') or 0); tp=float(pos.get('target') or 0); sl=float(pos.get('stop') or 0)
    except Exception: qty=tp=sl=0

    # Upgrade legacy gross-quantity positions from the original Binance BUY fills.
    if not bool(pos.get('quantity_fee_adjusted')) and pos.get('buy_order_id'):
        fee_meta=reconcile_state.buy_sellable_quantity(symbol,pos.get('buy_order_id'),pos.get('gross_quantity') or qty)
        if float(fee_meta.get('sellable_quantity') or 0)>0:
            qty=float(fee_meta['sellable_quantity'])
            trade_state.update_position(signal_id,quantity=qty,gross_quantity=fee_meta['gross_quantity'],
                                        base_commission=fee_meta['base_commission'],quantity_fee_adjusted=True,
                                        quantity_source=fee_meta['source'])
            pos=dict(pos); pos.update({'quantity':qty,'gross_quantity':fee_meta['gross_quantity'],
                                      'base_commission':fee_meta['base_commission'],'quantity_fee_adjusted':True,
                                      'quantity_source':fee_meta['source']})

    if qty<=0 or tp<=sl or sl<=0:
        _alert(f'🚨 {symbol} BUY confirmed but protection intent is incomplete. New entries stay blocked.')
        return

    # If the user or another external action sold/moved the bot-owned asset while
    # protection was missing, ownership is now ambiguous. Clear only bot STATE;
    # do not place/cancel/sell anything against the remaining wallet balance.
    external_sell=0.0
    try: external_sell=reconcile_state.sell_qty_since(symbol,float(pos.get('opened_at') or 0))
    except Exception: external_sell=0.0
    try: free_qty=reconcile_state.spot_free_balance(symbol[:-4])
    except Exception: free_qty=None
    tolerance=max(1e-12,qty*0.00025)
    if external_sell>tolerance or (free_qty is not None and free_qty+tolerance<qty):
        trade_state.mark_externally_closed(signal_id,reason='MANUAL_OR_EXTERNAL_EXIT_DETECTED',
                                           expected_bot_qty=qty,spot_free_qty=free_qty,
                                           sell_qty_since_open=external_sell)
        _alert(f'ℹ️ {symbol} manual/external exit detected. Stale bot position cleared; no order was placed.')
        print(f'[execution-recovery] EXTERNAL_EXIT {symbol} signal={signal_id} expected={qty:.10g} free={free_qty} sold_since={external_sell:.10g}',flush=True)
        return

    try: attempt=max(0,int(pos.get('protection_attempt') or 0))+1
    except Exception: attempt=1
    if attempt>3:
        _alert(f'🚨 {symbol} protection retry limit reached. New entries remain blocked until reconciled.')
        return
    trade_state.update_position(signal_id,protection_attempt=attempt)
    # Conservative stop-limit offset; Binance filter normalizer snaps it to tick size.
    sl_limit=sl*0.997
    body={'signal_id':signal_id,'action':'OCO','symbol':symbol,'quantity':qty,
          'take_profit_price':tp,'model_take_profit_price':pos.get('model_target'),
          'stop_loss_price':sl,'stop_limit_price':sl_limit,'protection_attempt':attempt,
          'confirmed':True,'dry_run':False,'timestamp':int(time.time())}
    try:
        code,row=_signed_local_oco(body)
        if code<300 and row.get('ok') is True:
            print(f'[execution-recovery] protection restored {symbol} signal={signal_id} attempt={attempt}',flush=True)
        else:
            _alert(f'🚨 Protection recovery failed for {symbol}: {row.get("status") or code}. New entries remain blocked.')
    except Exception as exc:
        _alert(f'🚨 Protection recovery transport failure for {symbol}: {type(exc).__name__}. New entries remain blocked.')
'''
e = _replace_once(e, old_recover, new_recover, 'ownership-safe recovery')
compile(e, str(epp), 'exec')
epp.write_text(e, encoding='utf-8')

# Build-time invariant checks for this exact incident class.
for path_, markers in {
    rp:['def buy_sellable_quantity(','def spot_free_balance(','def sell_qty_since('],
    tp:['gross_quantity','quantity_fee_adjusted','def release_terminal_reservation(','def mark_externally_closed('],
    fp:['tracked_quantity_cap','sellable_qty_source','protection_key=signal_id'],
    epp:['MANUAL_OR_EXTERNAL_EXIT_DETECTED','protection retry limit reached','release_terminal_reservation'],
}.items():
    src=path_.read_text(encoding='utf-8')
    for marker in markers:
        if marker not in src: raise SystemExit(f'fee-safe-oco invariant missing {path_.name}: {marker}')
    compile(src,str(path_),'exec')

print('[fee-safe-oco] OK base-fee net qty + ownership cap + terminal OCO retry + manual-exit stale-state cleanup')
