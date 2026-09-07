import crypto from "node:crypto";

const API_BASES = {
  production: [
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
  ],
  demo: ["https://demo-api.binance.com"],
  testnet: ["https://testnet.binance.vision"],
};

const ALLOWED = new Set([
  "GET /api/v3/account",
  "POST /api/v3/order",
  "POST /api/v3/orderList/oco",
]);

function json(res, status, body) {
  res.status(status).json(body);
}

function timingSafeHexEqual(a, b) {
  if (!/^[a-f0-9]{64}$/i.test(a || "") || !/^[a-f0-9]{64}$/i.test(b || "")) return false;
  const aa = Buffer.from(a, "hex");
  const bb = Buffer.from(b, "hex");
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function verifyRelay(req, raw) {
  const secret = process.env.TELEGRAM_BOT_TOKEN;
  if (!secret) return { ok: false, reason: "relay secret unavailable" };
  const ts = String(req.headers["x-executor-timestamp"] || "");
  const signature = String(req.headers["x-executor-signature"] || "").toLowerCase();
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) {
    return { ok: false, reason: "stale relay timestamp" };
  }
  const expected = crypto.createHmac("sha256", secret).update(`${ts}.${raw}`).digest("hex");
  return timingSafeHexEqual(signature, expected) ? { ok: true } : { ok: false, reason: "bad relay signature" };
}

function safeRequest(body) {
  const method = String(body?.method || "").toUpperCase();
  const path = String(body?.path || "");
  const apiKey = String(body?.apiKey || "");
  const query = String(body?.query || "");
  const network = String(body?.network || "production").toLowerCase();
  const route = `${method} ${path}`;
  if (!ALLOWED.has(route)) return { ok: false, status: "ROUTE_NOT_ALLOWED" };
  if (!Object.hasOwn(API_BASES, network)) return { ok: false, status: "NETWORK_NOT_ALLOWED" };
  if (network === "demo" && route !== "GET /api/v3/account") {
    return { ok: false, status: "DEMO_ROUTE_READ_ONLY" };
  }
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)) return { ok: false, status: "BAD_API_KEY" };
  if (!query || query.length > 4000) return { ok: false, status: "BAD_QUERY" };
  const qs = new URLSearchParams(query);
  const timestamp = Number(qs.get("timestamp"));
  const recvWindow = Number(qs.get("recvWindow") || 5000);
  const signature = String(qs.get("signature") || "");
  if (!Number.isFinite(timestamp) || Math.abs(Date.now() - timestamp) > Math.max(60_000, recvWindow + 10_000)) {
    return { ok: false, status: "STALE_BINANCE_REQUEST" };
  }
  if (!/^[a-f0-9]{64}$/i.test(signature)) return { ok: false, status: "BAD_BINANCE_SIGNATURE_FORMAT" };
  return { ok: true, method, path, apiKey, query, network };
}

function safeMessage(value) {
  return String(value || "").replace(/[A-Za-z0-9_-]{20,}/g, "[redacted]").slice(0, 240);
}

export default async function handler(req, res) {
  if (req.method !== "POST") return json(res, 405, { ok: false, status: "METHOD_NOT_ALLOWED" });
  try {
    const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    const auth = verifyRelay(req, raw);
    if (!auth.ok) return json(res, 401, { ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const parsed = safeRequest(JSON.parse(raw || "{}"));
    if (!parsed.ok) return json(res, 400, parsed);

    let last = { status: 502, code: null, msg: "Binance unavailable" };
    for (const base of API_BASES[parsed.network]) {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 8000);
        let r;
        try {
          r = await fetch(`${base}${parsed.path}?${parsed.query}`, {
            method: parsed.method,
            headers: {
              "X-MBX-APIKEY": parsed.apiKey,
              "content-type": "application/x-www-form-urlencoded",
            },
            signal: controller.signal,
          });
        } finally {
          clearTimeout(timer);
        }
        const text = await r.text();
        let data = {};
        try { data = JSON.parse(text || "{}"); } catch { data = { raw: text.slice(0, 500) }; }
        if (r.ok && !(Number(data?.code) < 0)) {
          return json(res, 200, { ok: true, status: "BINANCE_RELAY_OK", network: parsed.network, data });
        }
        last = { status: r.status, code: data?.code ?? null, msg: data?.msg || text.slice(0, 500) };
        console.warn("[binance-signed-relay] upstream", JSON.stringify({
          host: new URL(base).host,
          network: parsed.network,
          method: parsed.method,
          path: parsed.path,
          status: last.status,
          code: last.code,
          msg: safeMessage(last.msg),
          apiKeyLength: parsed.apiKey.length,
          queryLength: parsed.query.length,
        }));
        if (Number(last.code) < 0 && [-1022, -2014, -2015].includes(Number(last.code))) break;
      } catch (e) {
        last = { status: 502, code: null, msg: String(e?.message || e) };
        console.warn("[binance-signed-relay] transport", JSON.stringify({
          host: new URL(base).host,
          network: parsed.network,
          method: parsed.method,
          path: parsed.path,
          status: last.status,
          msg: safeMessage(last.msg),
        }));
      }
    }
    return json(res, 502, {
      ok: false,
      status: "BINANCE_RELAY_FAILED",
      network: parsed.network,
      upstream: { status: last.status, code: last.code, msg: safeMessage(last.msg) },
    });
  } catch (e) {
    return json(res, 500, { ok: false, status: "RELAY_ERROR", reason: safeMessage(e?.message || e) });
  }
}
