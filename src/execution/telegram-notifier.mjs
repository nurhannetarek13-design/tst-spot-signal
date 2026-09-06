function eventText(event = {}) {
  const type = String(event.type || event.event || 'EXECUTION_EVENT');
  const symbol = String(event.symbol || event.payload?.symbol || '').toUpperCase();
  const reason = event.reason || event.payload?.reason;
  const lines = [`🤖 ${type}${symbol ? ` ${symbol}` : ''}`];
  if (reason) lines.push(`Reason: ${reason}`);
  if (event.averagePrice || event.payload?.averagePrice) lines.push(`Price: ${event.averagePrice || event.payload.averagePrice}`);
  if (event.quantity || event.payload?.quantity) lines.push(`Qty: ${event.quantity || event.payload.quantity}`);
  if (event.stopPrice || event.payload?.stopPrice) lines.push(`Stop: ${event.stopPrice || event.payload.stopPrice}`);
  if (event.takeProfitPrice || event.payload?.takeProfitPrice) lines.push(`Target: ${event.takeProfitPrice || event.payload.takeProfitPrice}`);
  return lines.join('\n');
}

export function createTelegramNotifier({ token, chatId, fetchImpl = globalThis.fetch } = {}) {
  const botToken = String(token || '');
  const targetChat = String(chatId || '');
  return {
    configured: Boolean(botToken && targetChat),
    async notify(event) {
      if (!botToken || !targetChat) return { sent: false, reason: 'TELEGRAM_NOT_CONFIGURED' };
      if (typeof fetchImpl !== 'function') throw new Error('FETCH_REQUIRED');
      const response = await fetchImpl(`https://api.telegram.org/bot${botToken}/sendMessage`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ chat_id: targetChat, text: eventText(event), disable_web_page_preview: true }),
      });
      if (!response.ok) throw new Error(`TELEGRAM_SEND_FAILED_${response.status}`);
      return { sent: true };
    },
  };
}

export { eventText as formatTelegramExecutionEvent };
