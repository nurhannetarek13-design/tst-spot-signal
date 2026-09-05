import test from "node:test";
import assert from "node:assert/strict";
import { normalizeOctoBotSignal, gateExternalSignal } from "../scripts/octobot-signal-gateway.mjs";

test("normalizes OctoBot long signal into canonical spot BUY", () => {
  const now = Date.UTC(2026, 8, 6, 1, 0, 0);
  const signal = normalizeOctoBotSignal({
    id: "abc-1",
    pair: "SOL/USDT",
    signal: "LONG",
    confidence: 92,
    timestamp: now,
    price: 103.5,
    stop_loss: 101,
    take_profit: 108,
    timeframe: "15m",
    strategy: "octobot-test",
  }, now);

  assert.equal(signal.schemaVersion, 1);
  assert.equal(signal.source, "OCTOBOT");
  assert.equal(signal.symbol, "SOLUSDT");
  assert.equal(signal.marketType, "SPOT");
  assert.equal(signal.action, "BUY");
  assert.equal(signal.confidence, 0.92);
  assert.equal(signal.referencePrice, 103.5);
  assert.equal(signal.stopLoss, 101);
  assert.equal(signal.takeProfit, 108);
  assert.deepEqual(gateExternalSignal(signal, now), { pass: true, reason: "ACCEPTED_FOR_RESEARCH" });
});

test("rejects low-confidence signal", () => {
  const now = Date.now();
  const signal = normalizeOctoBotSignal({ pair: "ETHUSDT", action: "BUY", confidence: 0.5, timestamp: now }, now);
  assert.deepEqual(gateExternalSignal(signal, now), { pass: false, reason: "LOW_CONFIDENCE" });
});

test("rejects stale signal", () => {
  const now = Date.now();
  const signal = normalizeOctoBotSignal({ pair: "BTCUSDT", action: "BUY", confidence: 0.95, timestamp: now - 10 * 60_000 }, now);
  assert.deepEqual(gateExternalSignal(signal, now), { pass: false, reason: "STALE_OR_FUTURE_SIGNAL" });
});

test("rejects non-USDT and HOLD", () => {
  const now = Date.now();
  const nonUsdt = normalizeOctoBotSignal({ pair: "SOL/BTC", action: "BUY", confidence: 0.95, timestamp: now }, now);
  assert.equal(gateExternalSignal(nonUsdt, now).reason, "UNSUPPORTED_SYMBOL");

  const hold = normalizeOctoBotSignal({ pair: "SOLUSDT", action: "HOLD", confidence: 0.95, timestamp: now }, now);
  assert.equal(gateExternalSignal(hold, now).reason, "HOLD");
});
