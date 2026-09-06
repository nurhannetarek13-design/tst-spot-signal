import { applyClosedTradeToState } from './spot-execution-shell.mjs';
import { appendExecutionEvent } from './execution-state-store.mjs';
import { buildExecutionEvent } from './execution-events.mjs';

export async function settlePaperPositions({ stateStore, priceBySymbol = {}, now = Date.now(), barMinutes = 15, roundTripCost = 0.0036, notifier } = {}) {
  if (!stateStore?.load || !stateStore?.save) throw new Error('STATE_STORE_REQUIRED');
  let state = await stateStore.load();
  const closed = [];

  for (const [symbol, p] of Object.entries(state.positions || {})) {
    const px = Number(priceBySymbol[symbol]);
    if (!Number.isFinite(px) || px <= 0) continue;
    let reason = null;
    if (px <= Number(p.stopPrice)) reason = 'STOP';
    else if (px >= Number(p.takeProfitPrice)) reason = 'TARGET';
    else if (p.maxHoldBars && p.openedAt) {
      const ageBars = Math.floor((now - new Date(p.openedAt).getTime()) / (barMinutes * 60 * 1000));
      if (ageBars >= Number(p.maxHoldBars)) reason = 'TIME';
    }
    if (!reason) continue;

    const qty = Number(p.quantity);
    const entry = Number(p.averagePrice);
    const gross = (px - entry) * qty;
    const costs = (entry * qty + px * qty) * (roundTripCost / 2);
    const pnl = gross - costs;
    state = applyClosedTradeToState(state, { symbol, realizedPnlUsdt: pnl });
    const positions = { ...(state.positions || {}) };
    delete positions[symbol];
    state.positions = positions;
    const event = buildExecutionEvent('PAPER_POSITION_CLOSED', { symbol, reason, exitPrice: px, realizedPnlUsdt: pnl });
    state = appendExecutionEvent(state, event);
    closed.push({ symbol, reason, exitPrice: px, realizedPnlUsdt: pnl });
    if (notifier?.notify) await notifier.notify(event);
  }

  await stateStore.save(state);
  return { closed, state };
}
