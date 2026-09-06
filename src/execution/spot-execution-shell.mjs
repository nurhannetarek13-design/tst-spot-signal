export const DEFAULT_EXECUTION_POLICY = Object.freeze({
  liveTrading: false,
  paperMode: true,
  maxDailyLossUsdt: 2,
  maxOpenPositions: 3,
  maxQuotePerTradeUsdt: 10,
  requireProtectiveExit: true,
});

function finitePositive(value, name) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`${name} must be > 0`);
  return n;
}

export function assertCanOpenPosition({ policy = DEFAULT_EXECUTION_POLICY, state = {}, order = {} }) {
  const realizedPnlToday = Number(state.realizedPnlTodayUsdt || 0);
  const openPositions = Number(state.openPositions || 0);
  const quoteAmount = finitePositive(order.quoteAmountUsdt, 'quoteAmountUsdt');

  if (realizedPnlToday <= -Math.abs(Number(policy.maxDailyLossUsdt))) {
    throw new Error('DAILY_LOSS_CAP_REACHED');
  }
  if (openPositions >= Number(policy.maxOpenPositions)) {
    throw new Error('MAX_OPEN_POSITIONS_REACHED');
  }
  if (quoteAmount > Number(policy.maxQuotePerTradeUsdt)) {
    throw new Error('MAX_TRADE_SIZE_EXCEEDED');
  }
  if (policy.requireProtectiveExit && (!order.stopPrice || !order.takeProfitPrice)) {
    throw new Error('PROTECTIVE_EXIT_REQUIRED');
  }
  if (Number(order.stopPrice) >= Number(order.entryPrice)) {
    throw new Error('INVALID_STOP_PRICE');
  }
  if (Number(order.takeProfitPrice) <= Number(order.entryPrice)) {
    throw new Error('INVALID_TAKE_PROFIT_PRICE');
  }
  return true;
}

export function normalizeSpotIntent(intent) {
  const symbol = String(intent.symbol || '').toUpperCase();
  if (!/^[A-Z0-9]{5,20}$/.test(symbol) || !symbol.endsWith('USDT')) {
    throw new Error('INVALID_SPOT_SYMBOL');
  }
  const entryPrice = finitePositive(intent.entryPrice, 'entryPrice');
  const quoteAmountUsdt = finitePositive(intent.quoteAmountUsdt, 'quoteAmountUsdt');
  const stopPrice = finitePositive(intent.stopPrice, 'stopPrice');
  const takeProfitPrice = finitePositive(intent.takeProfitPrice, 'takeProfitPrice');

  return {
    symbol,
    side: 'BUY',
    entryPrice,
    quoteAmountUsdt,
    stopPrice,
    takeProfitPrice,
    clientTag: String(intent.clientTag || 'tst-spot-shell-v1'),
  };
}

export function buildExecutionPlan({ intent, policy = DEFAULT_EXECUTION_POLICY, state = {} }) {
  const order = normalizeSpotIntent(intent);
  assertCanOpenPosition({ policy, state, order });

  const liveAuthorized = policy.liveTrading === true && policy.paperMode === false;
  return {
    authorization: liveAuthorized ? 'LIVE_AUTHORIZED' : 'PAPER_ONLY',
    liveTrading: liveAuthorized,
    entry: {
      type: 'MARKET_QUOTE',
      symbol: order.symbol,
      quoteAmountUsdt: order.quoteAmountUsdt,
    },
    protection: {
      required: true,
      type: 'OCO_AFTER_FILL',
      stopPrice: order.stopPrice,
      takeProfitPrice: order.takeProfitPrice,
    },
    metadata: { clientTag: order.clientTag },
  };
}

export function applyFillToState(state = {}, fill = {}) {
  const next = { ...state };
  next.openPositions = Number(next.openPositions || 0) + 1;
  next.lastFill = {
    symbol: String(fill.symbol || '').toUpperCase(),
    quantity: finitePositive(fill.quantity, 'quantity'),
    averagePrice: finitePositive(fill.averagePrice, 'averagePrice'),
    at: fill.at || new Date().toISOString(),
  };
  return next;
}

export function applyClosedTradeToState(state = {}, trade = {}) {
  const next = { ...state };
  const pnl = Number(trade.realizedPnlUsdt);
  if (!Number.isFinite(pnl)) throw new Error('realizedPnlUsdt must be finite');
  next.openPositions = Math.max(0, Number(next.openPositions || 0) - 1);
  next.realizedPnlTodayUsdt = Number(next.realizedPnlTodayUsdt || 0) + pnl;
  next.lastClosedTrade = {
    symbol: String(trade.symbol || '').toUpperCase(),
    realizedPnlUsdt: pnl,
    at: trade.at || new Date().toISOString(),
  };
  return next;
}
