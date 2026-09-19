"""Auditable PAPER-only event journal and per-strategy metrics.

Never changes signals, limits, position sizing or order execution. Stats start at
installation; historical trades are not invented or silently re-counted.
"""
from collections import Counter
from datetime import datetime, timezone

MAX_EVENTS = 1500


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def reconcile_legacy_locks(state):
    """Repair old code that locked a symbol even after a rejected entry.

    A symbol without an open position AND without a recorded closed trade has
    no evidence of any historical fill; clear only its stale `seen_symbol`.
    Keep every strategy-specific attempt (`seen`) so a rejected signal is not
    retried endlessly on the same completed candle. Never erase a plausible
    accepted or closed trade's symbol dedupe.
    """
    locks = state.setdefault('seen_symbol', {})
    opened = set(state.get('positions', {}))
    historically_traded = {t['symbol'] for t in state.get('closed_trades', []) if 'symbol' in t}
    repaired = []
    for symbol in list(locks):
        if symbol not in opened and symbol not in historically_traded:
            repaired.append(dict(symbol=symbol, bar_time=locks.pop(symbol), reason='LEGACY_REJECTED_SIGNAL_LOCK'))
    return repaired


def _append(state, kind, strategy, symbol, bar_time, reason, pnl=None):
    journal = state.setdefault('paper_audit', {
        'version': 1, 'started_at': utc_now(), 'events': [], 'stats': {},
    })
    if journal.get('version') != 1:
        raise RuntimeError('UNKNOWN_PAPER_AUDIT_VERSION')
    events = journal['events']
    identity = '|'.join(str(x) for x in (kind, strategy, symbol, bar_time, reason))
    if any(e['key'] == identity for e in events):
        return False
    event = dict(key=identity, at=utc_now(), kind=kind, strategy=strategy,
                 symbol=symbol, bar_time=bar_time, reason=reason)
    if pnl is not None:
        event['pnl_usdt'] = float(pnl)
    events.append(event)
    if len(events) > MAX_EVENTS:
        del events[:len(events)-MAX_EVENTS]
    stats = journal['stats'].setdefault(strategy or '_SYSTEM', dict(
        signals=0, opened=0, rejected=0, closed=0, pnl_usdt=0.0, reasons={}))
    if kind == 'SIGNAL':
        stats['signals'] += 1
    elif kind == 'OPEN':
        stats['opened'] += 1
    elif kind in ('REJECTED', 'DATA_BLOCK'):
        stats['rejected'] += 1
        stats['reasons'][reason] = stats['reasons'].get(reason, 0) + 1
    elif kind == 'CLOSED':
        stats['closed'] += 1
        stats['pnl_usdt'] += float(pnl)
    return True


def record_run(state, closed_before, repaired=None):
    """Call AFTER native_paper.run, before final state save. Idempotent per event.

    Failed/suppressed candidates have no made-up fills. Missing historical
    rejection bar_time is linked only to an observed same-run signal, otherwise
    data/system failures are scoped per UTC day, rather than spammed every 5m.
    """
    if not isinstance(closed_before, int) or closed_before < 0:
        raise RuntimeError('INVALID_TRADE_AUDIT_BASELINE')
    state.setdefault('paper_audit', dict(version=1, started_at=utc_now(), events=[], stats={}))
    run_date = state.get('day', utc_now()[:10])
    by_pair = {}
    for signal in state.get('signals', []):
        name, symbol, bar = signal['strategy'], signal['symbol'], signal['bar_time']
        by_pair[(name, symbol)] = bar
        _append(state, 'SIGNAL', name, symbol, bar, signal.get('reason', 'SIGNAL'))
    for rejection in state.get('blocked', []):
        name = rejection.get('strategy', '_SYSTEM')
        symbol = rejection.get('symbol', '_ALL')
        bar = by_pair.get((name, symbol), run_date)
        _append(state, 'REJECTED' if name != '_SYSTEM' else 'DATA_BLOCK',
                name, symbol, bar, rejection.get('reason', 'UNKNOWN'))
    for accepted in state.get('accepted_signals', []):
        _append(state, 'OPEN', accepted['strategy'], accepted['symbol'],
                accepted['bar_time'], 'PAPER_OPENED')
    for trade in state.get('closed_trades', [])[closed_before:]:
        _append(state, 'CLOSED', trade['strategy'], trade['symbol'],
                trade.get('opened_at', '?'), trade.get('closed_at', '?'), trade['pnl_usdt'])
    for lock in repaired or []:
        _append(state, 'LOCK_REPAIRED', '_SYSTEM', lock['symbol'],
                lock['bar_time'], lock['reason'])
    report = strategy_report(state)
    state['paper_report'] = report
    return report


def strategy_report(state):
    """All-time metrics since audit installation, not an edge or profit verdict."""
    journal = state.get('paper_audit', {})
    stats = journal.get('stats', {})
    registry = state.get('source_registry', [])
    by_id = []
    for source in registry:
        metrics = stats.get(source['id'], {})
        by_id.append(dict(id=source['id'], status=source['status'],
            signals=metrics.get('signals', 0), opened=metrics.get('opened', 0),
            rejected=metrics.get('rejected', 0), closed=metrics.get('closed', 0),
            realized_pnl_usdt=round(metrics.get('pnl_usdt', 0.0), 8),
            rejection_reasons=metrics.get('reasons', {})))
    live_positions = Counter(p.get('strategy', '_UNKNOWN') for p in state.get('positions', {}).values())
    for row in by_id:
        row['currently_open'] = live_positions[row['id']]
    return dict(mode='PAPER_ONLY', since=journal.get('started_at'), generated_at=utc_now(),
                last_run=state.get('last_run'), wallet_cash_usdt=state.get('cash_usdt'),
                total_realized_pnl_from_audit_usdt=round(sum(x['realized_pnl_usdt'] for x in by_id), 8),
                by_strategy=by_id,
                notes='Counts start when audit was installed; tests do not prove an edge. Rejected is not a loss. Unapproved Freqtrade entries cannot trade.')
