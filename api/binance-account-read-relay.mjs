import crypto from "node:crypto";

const API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function json(res, status, body) {
  res.setHeader("Cache-Control", "no-store");
  return res.status(status).json(body);
}

function timingSafeHexEqual(a, b) {
  if (!/^[a-f0-9]{64}$/i.test(a || "") || !/^[a-f0-9]{64}$/i.test(b || "")) return false;
  const aa = Buffer.from(a, "hex");
  const bb = Buffer.from(b, "hex");
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function verifyCaller(req, raw) {
  const secret = process.env.TELEGRAM_BOT_TOKEN;
  if (!secret) return false;
  const ts = String(req.headers["x-sizing-timestamp"] || "");
  const sig = String(req.headers["x-sizing-signature"] || "").toLowerCase();
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${ts}.${raw}`).digest("hex");
  return timingSafeHexEqual(sig, expected);
}

function safePayload(body) {
  const apiKey = String(body?.apiKey || "");
  const query = String(body?.query || "");
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)) return null;
  if (!query || query.length > 2000) return null;
  const qs = new URLSearchParams(query);
  const timestamp = Number(qs.get("timestamp"));
  const recvWindow = Number(qs.get("recvWindow") || 5000);
  const signature = String(qs.get("signature") || "");
  if (!Number.isFinite(timestamp) || Math.abs(Date.now() - timestamp) > Math.max(60_000, recvWindow + 10_000)) return null;
  if (!/^[a-f0-9]{64}$/i.test(signature)) return null;
  return { apiKey, query };
}

export default async function handler(req, res) {
  if (req.method !== "POST") return json(res, 405, { ok: false, status: "METHOD_NOT_ALLOWED" });
  const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
  if (!verifyCaller(req, raw)) return json(res, 401, { ok: false, status: "UNAUTHORIZED" });
  const parsed = safePayload(JSON.parse(raw || "{}"));
  if (!parsed) return json(res, 400, { ok: false, status: "BAD_ACCOUNT_READ_REQUEST" });

  let last = { status: 502, code: null, msg: "Binance unavailable" };
  for (const base of API_BASES) {
    try {
      const r = await fetch(`${base}/api/v3/account?${parsed.query}`, {
        method: "GET",
        headers: { "X-MBX-APIKEY": parsed.apiKey, Accept: "application/json" },
        signal: AbortSignal.timeout(8000),
      });
      const text = await r.text();
      let data = {};
      try { data = JSON.parse(text || "{}"); } catch { data = {}; }
      if (r.ok && !(Number(data?.code) < 0)) {
        const usdt = (data.balances || []).find((x) => x.asset === "USDT") || { free: "0", locked: "0" };
        return json(res, 200, {
          ok: true,
          status: "ACCOUNT_READ_OK",
          canTrade: Boolean(data.canTrade),
          usdt: { free: Number(usdt.free || 0), locked: Number(usdt.locked || 0) },
          checkedAt: Date.now(),
        });
      }
      last = { status: r.status, code: data?.code ?? null, msg: data?.msg || "upstream rejected" };
      if (Number(last.code) < 0) break;
    } catch (e) {
      last = { status: 502, code: null, msg: String(e?.message || e) };
    }
  }
  return json(res, 502, { ok: false, status: "ACCOUNT_READ_FAILED", upstream: last });
}
