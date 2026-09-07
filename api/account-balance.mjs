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
  res.status(status).json(body);
}

function timingSafeHexEqual(a, b) {
  if (!/^[a-f0-9]{64}$/i.test(a || "") || !/^[a-f0-9]{64}$/i.test(b || "")) return false;
  const aa = Buffer.from(a, "hex");
  const bb = Buffer.from(b, "hex");
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function verifyRelay(req) {
  const secret = process.env.TELEGRAM_BOT_TOKEN;
  if (!secret) return { ok: false, reason: "relay secret unavailable" };
  const ts = String(req.headers["x-executor-timestamp"] || "");
  const signature = String(req.headers["x-executor-signature"] || "").toLowerCase();
  const stamp = Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) return { ok: false, reason: "stale relay timestamp" };
  const expected = crypto.createHmac("sha256", secret).update(`${ts}.balance`).digest("hex");
  return timingSafeHexEqual(signature, expected) ? { ok: true } : { ok: false, reason: "bad relay signature" };
}

async function signedBinance(method, path, params = {}) {
  const apiKey = process.env.BINANCE_API_KEY;
  const secret = process.env.BINANCE_API_SECRET;
  if (!apiKey || !secret) throw new Error("Binance API signing keys are missing in Vercel");
  const all = { ...params, recvWindow: 5000, timestamp: Date.now() };
  const qs = new URLSearchParams(Object.entries(all).map(([k, v]) => [k, String(v)])).toString();
  const signature = crypto.createHmac("sha256", secret).update(qs).digest("hex");
  let last = "unknown";
  for (const base of API_BASES) {
    try {
      const r = await fetch(`${base}${path}?${qs}&signature=${signature}`, {
        method,
        headers: { "X-MBX-APIKEY": apiKey },
      });
      const text = await r.text();
      const data = JSON.parse(text || "{}");
      if (r.ok && !(Number(data.code) < 0)) return data;
      last = `${data.code || r.status} ${data.msg || text}`;
    } catch (e) {
      last = String(e?.message || e);
    }
  }
  throw new Error(`Binance signed request failed: ${last}`);
}

export default async function handler(req, res) {
  if (req.method !== "GET") return json(res, 405, { ok: false, status: "METHOD_NOT_ALLOWED" });
  try {
    const auth = verifyRelay(req);
    if (!auth.ok) return json(res, 401, { ok: false, status: "UNAUTHORIZED", reason: auth.reason });

    const account = await signedBinance("GET", "/api/v3/account", {});
    const balances = (account.balances || [])
      .map((b) => ({ asset: b.asset, free: Number(b.free || 0), locked: Number(b.locked || 0) }))
      .filter((b) => b.free > 0 || b.locked > 0);
    const usdt = balances.find((b) => b.asset === "USDT") || { asset: "USDT", free: 0, locked: 0 };
    const sol = balances.find((b) => b.asset === "SOL") || { asset: "SOL", free: 0, locked: 0 };

    return json(res, 200, {
      ok: true,
      status: "ACCOUNT_BALANCE_OK",
      canTrade: Boolean(account.canTrade),
      usdt: { free: usdt.free, locked: usdt.locked, total: usdt.free + usdt.locked },
      sol: { free: sol.free, locked: sol.locked, total: sol.free + sol.locked },
      nonZeroAssets: balances,
      checkedAt: Date.now(),
    });
  } catch (e) {
    return json(res, 500, { ok: false, status: "BALANCE_ERROR", reason: String(e?.message || e) });
  }
}
