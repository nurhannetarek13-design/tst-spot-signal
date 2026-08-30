import express from "express";

const app = express();
const API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://data-api.binance.vision",
];

app.get("/", (_request, response) => {
  response.json({ ok: true, service: "tst-spot-signal-vercel-test", liveTrading: false });
});

app.get("/health", async (_request, response) => {
  const attempts = [];
  for (const base of API_BASES) {
    try {
      const upstream = await fetch(`${base}/api/v3/time`, {
        headers: { Accept: "application/json", "User-Agent": "tst-spot-signal-vercel/1.0" },
        signal: AbortSignal.timeout(10_000),
      });
      attempts.push({ base, status: upstream.status });
      if (!upstream.ok) continue;
      const data = await upstream.json();
      return response.json({ ok: true, binance: "reachable", endpoint: base, serverTime: data.serverTime, region: process.env.VERCEL_REGION || "unknown", liveTrading: false });
    } catch (error) {
      attempts.push({ base, error: error.message });
    }
  }
  return response.status(503).json({ ok: false, binance: "blocked", attempts, liveTrading: false });
});

export default app;
