import express from "express";

const app = express();
const port = Number(process.env.PORT || 3000);
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

app.get("/health", (_req, res) => res.json({ ok: true, service: "binance-public-proxy" }));
app.get("/api/binance-public", async (req, res) => {
  const raw = String(req.query.path || "");
  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { decoded = raw; }
  if (!decoded.startsWith("/api/v3/")) return res.status(400).json({ ok: false, error: "BAD_PATH" });
  const u = new URL(`https://local${decoded}`);
  if (!ALLOWED_PATHS.has(u.pathname)) return res.status(403).json({ ok: false, error: "PATH_NOT_ALLOWED" });

  let last = "unavailable";
  for (const base of BASES) {
    try {
      const r = await fetch(`${base}${u.pathname}${u.search}`, {
        headers: { Accept: "application/json", "User-Agent": "tst-railway-binance-proxy/1.0" },
        signal: AbortSignal.timeout(12000),
      });
      const text = await r.text();
      if (r.ok) {
        res.set("cache-control", "public, max-age=1, s-maxage=2");
        res.type("application/json").status(200).send(text);
        return;
      }
      last = `${r.status} ${text.slice(0, 300)}`;
    } catch (e) { last = String(e?.message || e); }
  }
  res.status(502).json({ ok: false, error: "BINANCE_UPSTREAM_FAILED", detail: last });
});

app.listen(port, "0.0.0.0", () => console.log(`binance-public-proxy listening on ${port}`));
