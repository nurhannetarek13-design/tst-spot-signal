import crypto from "node:crypto";
import http from "node:http";

const PORT = Number(process.env.OCTOBOT_GATEWAY_PORT || process.env.PORT || 10001);
const WEBHOOK_SECRET = process.env.OCTOBOT_WEBHOOK_SECRET || "";
const MIN_CONFIDENCE = Number(process.env.EXTERNAL_MIN_CONFIDENCE || 0.8);
const MAX_AGE_MS = Number(process.env.EXTERNAL_MAX_AGE_MS || 5 * 60_000);
const SIGNAL_TTL_MS = Number(process.env.EXTERNAL_SIGNAL_TTL_MS || 15 * 60_000);
const WORKER_URL = process.env.WORKER_URL || "https://tst-spot-signal.nurhanne-tarek13.workers.dev/";
const RELAY_SECRET = process.env.RELAY_SECRET || "";

const seen = new Map();
let latest = null;

function send(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(data));
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  if (raw.length > 256_000) throw new Error("payload_too_large");
  return { raw, json: JSON.parse(raw.toString("utf8") || "{}") };
}

function safeEqual(a, b) {
  const aa = Buffer.from(String(a || ""));
  const bb = Buffer.from(String(b || ""));
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function authorized(req, raw) {
  if (!WEBHOOK_SECRET) return false;
  const bearer = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  if (bearer && safeEqual(bearer, WEBHOOK_SECRET)) return true;
  const supplied = String(req.headers["x-octobot-signature"] || "");
  const expected = crypto.createHmac("sha256", WEBHOOK_SECRET).update(raw).digest("hex");
  return supplied && safeEqual(supplied, expected);
}

function number(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function normalizeAction(value) {
  const v = String(value || "").trim().toUpperCase();
  if (["BUY", "LONG", "ENTER_LONG", "STRONG_BUY"].includes(v)) return "BUY";
  if (["SELL", "EXIT_LONG", "CLOSE_LONG", "STRONG_SELL"].includes(v)) return "SELL";
  if (["HOLD", "NEUTRAL", "NONE", "WAIT"].includes(v)) return "HOLD";
  return null;
}

function normalizeSymbol(value) {
  return String(value || "").toUpperCase().replace(/[\/:_-]/g, "").replace(/PERP$/i, "");
}

export function normalizeOctoBotSignal(payload, now = Date.now()) {
  const source = "OCTOBOT";
  const symbol = normalizeSymbol(payload.symbol ?? payload.pair ?? payload.market ?? payload.ticker);
  const action = normalizeAction(payload.action ?? payload.signal ?? payload.side ?? payload.order_side ?? payload.state);
  let confidence = number(payload.confidence ?? payload.score ?? payload.strength ?? payload.probability);
  if (confidence !== null && confidence > 1 && confidence <= 100) confidence /= 100;
  if (confidence === null) confidence = 0;

  const rawTime = payload.timestamp ?? payload.emittedAt ?? payload.time ?? payload.created_at ?? now;
  const emittedAtMs = typeof rawTime === "number" ? (rawTime < 10_000_000_000 ? rawTime * 1000 : rawTime) : Date.parse(rawTime);
  if (!Number.isFinite(emittedAtMs)) throw new Error("invalid_timestamp");

  const signalId = String(payload.signalId ?? payload.id ?? payload.uuid ?? crypto.createHash("sha256")
    .update(JSON.stringify([source, symbol, action, confidence, emittedAtMs, payload.price ?? payload.entry]))
    .digest("hex").slice(0, 24));

  return {
    schemaVersion: 1,
    source,
    signalId,
    symbol,
    marketType: "SPOT",
    action,
    confidence,
    emittedAt: new Date(emittedAtMs).toISOString(),
    expiresAt: new Date(emittedAtMs + SIGNAL_TTL_MS).toISOString(),
    referencePrice: number(payload.price ?? payload.entry ?? payload.referencePrice),
    stopLoss: number(payload.stopLoss ?? payload.stop_loss ?? payload.sl),
    takeProfit: number(payload.takeProfit ?? payload.take_profit ?? payload.tp),
    timeframe: payload.timeframe ? String(payload.timeframe) : null,
    strategy: payload.strategy ? String(payload.strategy) : null,
    metadata: {
      evaluator: payload.evaluator ?? null,
      reason: payload.reason ?? payload.message ?? null,
    },
  };
}

export function gateExternalSignal(signal, now = Date.now()) {
  if (!signal.symbol || !signal.symbol.endsWith("USDT")) return { pass: false, reason: "UNSUPPORTED_SYMBOL" };
  if (!signal.action) return { pass: false, reason: "INVALID_ACTION" };
  if (signal.action === "HOLD") return { pass: false, reason: "HOLD" };
  if (signal.confidence < MIN_CONFIDENCE) return { pass: false, reason: "LOW_CONFIDENCE" };
  const emitted = Date.parse(signal.emittedAt);
  if (!Number.isFinite(emitted) || now - emitted > MAX_AGE_MS || emitted - now > 60_000) return { pass: false, reason: "STALE_OR_FUTURE_SIGNAL" };
  if (Date.parse(signal.expiresAt) <= now) return { pass: false, reason: "EXPIRED" };
  if (seen.has(signal.signalId)) return { pass: false, reason: "DUPLICATE" };
  return { pass: true, reason: "ACCEPTED_FOR_RESEARCH" };
}

function remember(signal, now = Date.now()) {
  seen.set(signal.signalId, now);
  latest = { signal, acceptedAt: new Date(now).toISOString() };
  for (const [id, ts] of seen) if (now - ts > 24 * 60 * 60_000) seen.delete(id);
}

function alertText(signal) {
  const lines = [
    `🧠 External signal: ${signal.source}`,
    `${signal.action} ${signal.symbol}`,
    `Confidence: ${(signal.confidence * 100).toFixed(1)}%`,
    signal.referencePrice ? `Reference: ${signal.referencePrice}` : null,
    signal.stopLoss ? `SL: ${signal.stopLoss}` : null,
    signal.takeProfit ? `TP: ${signal.takeProfit}` : null,
    signal.timeframe ? `TF: ${signal.timeframe}` : null,
    signal.strategy ? `Strategy: ${signal.strategy}` : null,
    "⚠️ RESEARCH/SIGNAL ONLY — no live order was placed.",
  ].filter(Boolean);
  return lines.join("\n");
}

async function relayTelegram(text) {
  if (!RELAY_SECRET) return { skipped: true, reason: "RELAY_SECRET_NOT_CONFIGURED" };
  const body = JSON.stringify({ timestamp: Date.now(), text });
  const signature = crypto.createHmac("sha256", RELAY_SECRET).update(body).digest("hex");
  const response = await fetch(new URL("?relay=telegram", WORKER_URL), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Relay-Signature": signature },
    body,
    signal: AbortSignal.timeout(10_000),
  });
  const raw = await response.text();
  if (!response.ok) throw new Error(`telegram_relay_${response.status}:${raw.slice(0, 120)}`);
  return JSON.parse(raw);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    try {
      if (url.pathname === "/health") {
        return send(res, 200, {
          ok: true,
          service: "octobot-external-signal-gateway",
          mode: "SIGNAL_ONLY",
          liveTrading: false,
          minConfidence: MIN_CONFIDENCE,
          maxAgeMs: MAX_AGE_MS,
          latest,
        });
      }
      if (url.pathname === "/webhook/octobot" && req.method === "POST") {
        const { raw, json } = await readJson(req);
        if (!authorized(req, raw)) return send(res, 401, { ok: false, error: "Unauthorized" });
        const signal = normalizeOctoBotSignal(json);
        const gate = gateExternalSignal(signal);
        if (!gate.pass) return send(res, gate.reason === "DUPLICATE" ? 200 : 422, { ok: false, gate, signal, liveTrading: false });
        remember(signal);
        const telegram = await relayTelegram(alertText(signal));
        return send(res, 202, { ok: true, gate, signal, telegram, mode: "SIGNAL_ONLY", liveTrading: false });
      }
      return send(res, 404, { ok: false, error: "Not found" });
    } catch (error) {
      return send(res, 400, { ok: false, error: error.message, liveTrading: false });
    }
  }).listen(PORT, "0.0.0.0", () => console.log(`octobot signal gateway listening on ${PORT}`));
}
