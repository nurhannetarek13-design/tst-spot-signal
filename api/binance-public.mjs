const ALLOWED_PATHS = new Set([
  "/api/v3/ticker/24hr",
  "/api/v3/ticker/bookTicker",
  "/api/v3/exchangeInfo",
  "/api/v3/klines",
  "/api/v3/depth",
  "/api/v3/trades",
  "/api/v3/aggTrades",
]);

const BASES = [
  "https://data-api.binance.vision",
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function send(res, status, body) {
  res.setHeader("cache-control", "s-maxage=2, stale-while-revalidate=3");
  res.status(status).json(body);
}

export default async function handler(req, res) {
  if (req.method !== "GET") return send(res, 405, { ok: false, error: "METHOD_NOT_ALLOWED" });

  const raw = String(req.query?.path || "");
  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { decoded = raw; }
  if (!decoded.startsWith("/api/v3/")) return send(res, 400, { ok: false, error: "BAD_PATH" });

  const u = new URL(`https://local${decoded}`);
  if (!ALLOWED_PATHS.has(u.pathname)) return send(res, 403, { ok: false, error: "PATH_NOT_ALLOWED" });

  let last = "unavailable";
  for (const base of BASES) {
    try {
      const r = await fetch(`${base}${u.pathname}${u.search}`, {
        headers: { Accept: "application/json", "User-Agent": "tst-vercel-binance-proxy/1.0" },
        signal: AbortSignal.timeout(12000),
      });
      const text = await r.text();
      if (r.ok) {
        res.setHeader("content-type", "application/json; charset=utf-8");
        res.setHeader("cache-control", "s-maxage=2, stale-while-revalidate=3");
        return res.status(200).send(text);
      }
      last = `${r.status} ${text.slice(0, 300)}`;
    } catch (e) {
      last = String(e?.message || e);
    }
  }

  return send(res, 502, { ok: false, error: "BINANCE_UPSTREAM_FAILED", detail: last });
}
