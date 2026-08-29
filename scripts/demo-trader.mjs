import crypto from "node:crypto";

const DEMO_BASE = "https://demo-api.binance.com";
const WORKER_URL = "https://tst-spot-signal.nurhanne-tarek13.workers.dev/";
const API_KEY = process.env.BINANCE_DEMO_API_KEY;
const SECRET_KEY = process.env.BINANCE_DEMO_SECRET_KEY;

if (!API_KEY || !SECRET_KEY) {
  throw new Error("Missing BINANCE_DEMO_API_KEY or BINANCE_DEMO_SECRET_KEY in GitHub Actions secrets");
}

function clean(value) {
  return Number(value).toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
}

function floorByIncrement(value, increment) {
  const step = Number(increment);
  const decimals = Math.max(0, (String(increment).split(".")[1] || "").replace(/0+$/, "").length);
  return Number((Math.floor((Number(value) + 1e-12) / step) * step).toFixed(decimals));
}

async function publicDemo(path) {
  const response = await fetch(DEMO_BASE + path, { headers: { Accept: "application/json" } });
  const raw = await response.text();
  let data;
  try { data = JSON.parse(raw); }
  catch { throw new Error(`Binance Demo HTTP ${response.status}: returned HTML instead of JSON`); }
  if (!response.ok || data.code) throw new Error(`Binance Demo ${data.code || response.status}: ${data.msg || "request failed"}`);
  return data;
}

async function signed(method, path, params = {}) {
  const query = new URLSearchParams({
    ...params,
    recvWindow: "10000",
    timestamp: String(Date.now()),
  });
  const signature = crypto.createHmac("sha256", SECRET_KEY).update(query.toString()).digest("hex");
  query.set("signature", signature);

  const url = method === "GET" ? `${DEMO_BASE}${path}?${query}` : `${DEMO_BASE}${path}`;
  const response = await fetch(url, {
    method,
    headers: {
      "X-MBX-APIKEY": API_KEY,
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: method === "GET" ? undefined : query.toString(),
  });
  const raw = await response.text();
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new Error(`Binance Demo HTTP ${response.status}: returned HTML instead of JSON`);
  }
  if (!response.ok || data.code) {
    throw new Error(`Binance Demo ${data.code || response.status}: ${data.msg || "request failed"}`);
  }
  return data;
}

async function main() {
  const response = await fetch(WORKER_URL, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Scanner HTTP ${response.status}`);
  const scan = await response.json();

  if (scan.status !== "VALID_SETUP_FOUND" || !scan.selected?.valid || !scan.selected?.plan) {
    console.log("No valid setup. No demo order placed.");
    return;
  }

  const setup = scan.selected;
  const plan = setup.plan;
  if (Number(plan.positionUSDT) > 10.0001 || Number(plan.riskIncludingFeesUSDT) > 0.5001) {
    throw new Error("Risk guard rejected this setup");
  }
  if (Number(plan.netRewardRisk) < 2) throw new Error("Reward/risk guard rejected this setup");

  const openOrders = await signed("GET", "/api/v3/openOrders");
  if (Array.isArray(openOrders) && openOrders.length > 0) {
    console.log("Existing demo order found. Skipping.");
    return;
  }

  const safeId = crypto.createHash("sha256")
    .update(`${setup.symbol}|${setup.signalId}`)
    .digest("hex").slice(0, 20);
  const clientId = `gh${safeId}`;

  const history = await signed("GET", "/api/v3/allOrders", {
    symbol: setup.symbol,
    limit: "50",
  });
  if (Array.isArray(history) && history.some(order => order.clientOrderId === clientId)) {
    console.log("This signal was already traded on Demo. Skipping.");
    return;
  }

  const exchangeInfo = await publicDemo(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(setup.symbol)}`);
  const symbolInfo = exchangeInfo.symbols?.[0];
  if (!symbolInfo) throw new Error("Symbol is unavailable on Binance Demo");
  const lot = symbolInfo.filters.find(filter => filter.filterType === "LOT_SIZE");
  const priceFilter = symbolInfo.filters.find(filter => filter.filterType === "PRICE_FILTER");
  const stepSize = lot?.stepSize || "0.00000001";
  const tickSize = priceFilter?.tickSize || "0.00000001";

  const workingQty = floorByIncrement(Number(plan.quantity), stepSize);
  const protectedQty = floorByIncrement(workingQty * 0.999, stepSize);
  const entryPrice = floorByIncrement(Number(plan.entry), tickSize);
  const stopPrice = floorByIncrement(Number(plan.stop), tickSize);
  const targetPrice = floorByIncrement(Number(plan.target1), tickSize);
  if (!(workingQty > 0) || !(protectedQty > 0)) throw new Error("Quantity is below Demo trading rules");

  const order = await signed("POST", "/api/v3/orderList/otoco", {
    symbol: setup.symbol,
    workingType: "LIMIT",
    workingSide: "BUY",
    workingPrice: clean(entryPrice),
    workingQuantity: clean(workingQty),
    workingTimeInForce: "GTC",
    workingClientOrderId: clientId,
    pendingSide: "SELL",
    pendingQuantity: clean(protectedQty),
    pendingAboveType: "LIMIT_MAKER",
    pendingAbovePrice: clean(targetPrice),
    pendingBelowType: "STOP_LOSS",
    pendingBelowStopPrice: clean(stopPrice),
    newOrderRespType: "RESULT",
  });

  console.log(JSON.stringify({
    demoOrderPlaced: true,
    symbol: setup.symbol,
    orderListId: order.orderListId,
    entry: clean(entryPrice),
    stop: clean(stopPrice),
    target: clean(targetPrice),
    quantity: clean(workingQty),
    liveTrading: false,
  }, null, 2));
}

main().catch(error => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
