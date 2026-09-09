import worker, { SignalState } from "./buy-gateway-time-monitor.js";
export { SignalState };

const FAST_INGEST_PUBLIC_SPKI_B64 = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEkUcYKimdOJky2cdmENTgw42L1xqZDCRf7dqbvndHYIowbKCMoNT2BL2e2Zi9uk2aMbFh2dos9x5OUaFPyMi2+g==";
const BINANCE_API_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function b64ToBytes(s) {
  const bin = atob(s);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}
function b64urlToBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  return b64ToBytes(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
}
async function importFastPublicKey() {
  return crypto.subtle.importKey(
    "spki",
    b64ToBytes(FAST_INGEST_PUBLIC_SPKI_B64),
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["verify"],
  );
}
async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function safeHexEqual(a, b) {
  const aa = String(a || "").toLowerCase();
  const bb = String(b || "").toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(aa) || !/^[a-f0-9]{64}$/.test(bb)) return false;
  let diff = 0;
  for (let i = 0; i < aa.length; i++) diff |= aa.charCodeAt(i) ^ bb.charCodeAt(i);
  return diff === 0;
}

async function verifyRailwayFastIngest(request, env, ctx) {
  const raw = await request.text();
  const ts = String(request.headers.get("x-fast-timestamp") || "");
  const supplied = String(request.headers.get("x-fast-ecdsa") || "");
  if (!Number.isFinite(Number(ts)) || Math.abs(Date.now() - Number(ts)) > 60000) {
    return Response.json({ ok: false, status: "STALE_INGEST", autoBuy: false }, { status: 401 });
  }
  let sig;
  try { sig = b64urlToBytes(supplied); } catch { sig = new Uint8Array(); }
  if (sig.length !== 64) {
    return Response.json({ ok: false, status: "BAD_FAST_INGEST_SIGNATURE", autoBuy: false }, { status: 401 });
  }
  const key = await importFastPublicKey();
  const message = new TextEncoder().encode(`${ts}.${raw}`);
  const valid = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, sig, message);
  if (!valid) {
    return Response.json({ ok: false, status: "BAD_FAST_INGEST_SIGNATURE", autoBuy: false }, { status: 401 });
  }
  if (!env.TELEGRAM_BOT_TOKEN) {
    return Response.json({ ok: false, status: "TELEGRAM_TOKEN_MISSING", autoBuy: false }, { status: 503 });
  }

  const headers = new Headers(request.headers);
  headers.set("x-fast-signature", await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`));
  headers.set("content-type", headers.get("content-type") || "application/json");
  const internalRequest = new Request(request.url, { method: "POST", headers, body: raw });
  return worker.fetch(internalRequest, env, ctx);
}

async function growthCapDryRun(env, ctx) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    return Response.json({ ok:false, status:"TELEGRAM_NOT_CONFIGURED" }, { status:503 });
  }
  const body = JSON.stringify({
    id: `growth-cap-${Date.now()}`,
    symbol: "牛来USDT",
    entry: 100,
    stop: 99,
    target: 102,
    stakeUSDT: 40,
    score: 100,
    strategy: "GROWTH_CAP_UNICODE_RUNTIME_PROBE",
    dryRun: true,
  });
  const ts = String(Date.now());
  const sig = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${body}`);
  const req = new Request("https://internal/fast-signal-ingest", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-fast-timestamp": ts,
      "x-fast-signature": sig,
      "cache-control": "no-store",
    },
    body,
  });
  const r = await worker.fetch(req, env, ctx);
  const text = await r.text();
  let row = {}; try { row = JSON.parse(text || "{}"); } catch { row = { ok:false, status:"NON_JSON" }; }
  return Response.json({
    ok: r.ok && row.ok === true && row.status === "FAST_SIGNAL_DRYRUN_OK" && Number(row.recommendedUSDT) === 40,
    status: row.status || `HTTP_${r.status}`,
    recommendedUSDT: row.recommendedUSDT ?? null,
    testedStakeUSDT: 40,
    testedSymbol: "牛来USDT",
    dryRun: true,
    autoBuy: false,
    noSecretValuesExposed: true,
  }, { status: r.ok ? 200 : r.status, headers: { "cache-control": "no-store" } });
}

async function readBinanceAccountRelay(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    return Response.json({ ok: false, status: "RELAY_AUTH_UNAVAILABLE" }, { status: 503 });
  }
  const raw = await request.text();
  const ts = String(request.headers.get("x-sizing-timestamp") || "");
  const supplied = String(request.headers.get("x-sizing-signature") || "");
  if (!Number.isFinite(Number(ts)) || Math.abs(Date.now() - Number(ts)) > 60000) {
    return Response.json({ ok: false, status: "STALE_ACCOUNT_READ" }, { status: 401 });
  }
  const expected = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`);
  if (!safeHexEqual(supplied, expected)) {
    return Response.json({ ok: false, status: "BAD_ACCOUNT_READ_SIGNATURE" }, { status: 401 });
  }

  let body;
  try { body = JSON.parse(raw || "{}"); } catch { body = {}; }
  const apiKey = String(body?.apiKey || "");
  const query = String(body?.query || "");
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey) || !query || query.length > 2000) {
    return Response.json({ ok: false, status: "BAD_ACCOUNT_READ_REQUEST" }, { status: 400 });
  }
  const qs = new URLSearchParams(query);
  const stamp = Number(qs.get("timestamp"));
  const recvWindow = Number(qs.get("recvWindow") || 5000);
  const binanceSig = String(qs.get("signature") || "");
  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > Math.max(60000, recvWindow + 10000) || !/^[a-f0-9]{64}$/i.test(binanceSig)) {
    return Response.json({ ok: false, status: "BAD_BINANCE_ACCOUNT_SIGNATURE" }, { status: 400 });
  }

  let last = { status: 502, code: null, msg: "Binance unavailable" };
  for (const base of BINANCE_API_BASES) {
    try {
      const r = await fetch(`${base}/api/v3/account?${query}`, {
        method: "GET",
        headers: { "X-MBX-APIKEY": apiKey, Accept: "application/json" },
      });
      const text = await r.text();
      let data = {};
      try { data = JSON.parse(text || "{}"); } catch { data = {}; }
      if (r.ok && !(Number(data?.code) < 0)) {
        const usdt = (data.balances || []).find((x) => x.asset === "USDT") || { free: "0", locked: "0" };
        return Response.json({
          ok: true,
          status: "ACCOUNT_READ_OK",
          canTrade: Boolean(data.canTrade),
          usdt: { free: Number(usdt.free || 0), locked: Number(usdt.locked || 0) },
          checkedAt: Date.now(),
        }, { headers: { "Cache-Control": "no-store" } });
      }
      last = { status: r.status, code: data?.code ?? null, msg: String(data?.msg || "upstream rejected").slice(0, 160) };
      if (Number(last.code) < 0) break;
    } catch (e) {
      last = { status: 502, code: null, msg: String(e?.message || e).slice(0, 160) };
    }
  }
  return Response.json({ ok: false, status: "ACCOUNT_READ_FAILED", upstream: last }, { status: 502 });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/growth-cap-check" && request.method === "GET") {
      return growthCapDryRun(env, ctx);
    }
    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") {
      return verifyRailwayFastIngest(request, env, ctx);
    }
    if (url.pathname === "/account-read-relay" && request.method === "POST") {
      return readBinanceAccountRelay(request, env);
    }
    return worker.fetch(request, env, ctx);
  },
  async scheduled(event, env, ctx) {
    return worker.scheduled(event, env, ctx);
  },
};
