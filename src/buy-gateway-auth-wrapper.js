import worker, { SignalState } from "./buy-gateway-time-monitor.js";
export { SignalState };

const FAST_INGEST_PUBLIC_SPKI_B64 = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEkUcYKimdOJky2cdmENTgw42L1xqZDCRf7dqbvndHYIowbKCMoNT2BL2e2Zi9uk2aMbFh2dos9x5OUaFPyMi2+g==";

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

  // The canonical worker still performs its original HMAC verification. Re-sign
  // internally with the Worker's own Telegram secret only after Railway's ECDSA
  // identity has been verified. This removes cross-service Telegram-token coupling.
  const headers = new Headers(request.headers);
  headers.set("x-fast-signature", await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`));
  headers.set("content-type", headers.get("content-type") || "application/json");
  const internalRequest = new Request(request.url, { method: "POST", headers, body: raw });
  return worker.fetch(internalRequest, env, ctx);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") {
      return verifyRailwayFastIngest(request, env, ctx);
    }
    return worker.fetch(request, env, ctx);
  },
  async scheduled(event, env, ctx) {
    return worker.scheduled(event, env, ctx);
  },
};
