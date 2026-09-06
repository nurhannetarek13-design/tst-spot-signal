function positive(value, name) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`${name} must be > 0`);
  return n;
}

export function createMockBinanceSpotAdapter({ priceBySymbol = {} } = {}) {
  let nextOrderId = 1;
  const orders = [];

  return {
    async placeMarketBuy({ symbol, quoteAmountUsdt, referencePrice }) {
      const price = positive(priceBySymbol[symbol] ?? referencePrice, 'fillPrice');
      const quote = positive(quoteAmountUsdt, 'quoteAmountUsdt');
      const fill = {
        orderId: `mock-entry-${nextOrderId++}`,
        symbol,
        status: 'FILLED',
        averagePrice: price,
        quantity: quote / price,
        quoteSpent: quote,
      };
      orders.push({ type: 'MARKET_BUY', ...fill });
      return fill;
    },

    async placeOcoSell({ symbol, quantity, stopPrice, takeProfitPrice }) {
      const oco = {
        orderListId: `mock-oco-${nextOrderId++}`,
        symbol,
        status: 'ACTIVE',
        quantity: positive(quantity, 'quantity'),
        stopPrice: positive(stopPrice, 'stopPrice'),
        takeProfitPrice: positive(takeProfitPrice, 'takeProfitPrice'),
      };
      if (oco.stopPrice >= oco.takeProfitPrice) throw new Error('INVALID_OCO_PRICES');
      orders.push({ type: 'OCO_SELL', ...oco });
      return oco;
    },

    async listOrders() {
      return structuredClone(orders);
    },
  };
}
