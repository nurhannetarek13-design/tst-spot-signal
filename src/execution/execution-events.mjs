function fmt(n, digits = 4) {
  const x = Number(n);
  return Number.isFinite(x) ? x.toFixed(digits) : 'n/a';
}

export function buildExecutionEvent(type, payload = {}) {
  const event = {
    type,
    at: payload.at || new Date().toISOString(),
    symbol: payload.symbol ? String(payload.symbol).toUpperCase() : undefined,
    ...payload,
  };
  return event;
}

export function formatTelegramExecutionEvent(event) {
  switch (event.type) {
    case 'PAPER_ENTRY_FILLED':
      return `🧪 PAPER BUY ${event.symbol}\nQty: ${fmt(event.quantity, 8)}\nAvg: ${fmt(event.averagePrice)} USDT\nSpent: ${fmt(event.quoteSpent, 2)} USDT`;
    case 'PROTECTION_PLACED':
      return `🛡️ Protection ${event.symbol}\nStop: ${fmt(event.stopPrice)}\nTake Profit: ${fmt(event.takeProfitPrice)}`;
    case 'TRADE_CLOSED':
      return `${Number(event.realizedPnlUsdt) >= 0 ? '✅' : '❌'} ${event.symbol} closed\nPnL: ${fmt(event.realizedPnlUsdt, 4)} USDT\nDaily PnL: ${fmt(event.realizedPnlTodayUsdt, 4)} USDT`;
    case 'EXECUTION_REJECTED':
      return `⛔ Execution rejected${event.symbol ? ` ${event.symbol}` : ''}\nReason: ${event.reason || 'UNKNOWN'}`;
    default:
      return `ℹ️ ${event.type || 'EXECUTION_EVENT'}${event.symbol ? ` ${event.symbol}` : ''}`;
  }
}

export function createTelegramNotifier({ sendMessage } = {}) {
  return {
    async notify(event) {
      const text = formatTelegramExecutionEvent(event);
      if (typeof sendMessage === 'function') await sendMessage(text, event);
      return text;
    },
  };
}
