function pemBody(pem, label) {
  return String(pem || "")
    .replace(new RegExp(`-----BEGIN ${label}-----`, "g"), "")
    .replace(new RegExp(`-----END ${label}-----`, "g"), "")
    .replace(/\s+/g, "");
}

function base64ToBytes(base64) {
  const raw = atob(String(base64 || ""));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

function bytesToBase64(bytes) {
  const arr = new Uint8Array(bytes);
  let raw = "";
  for (let i = 0; i < arr.length; i++) raw += String.fromCharCode(arr[i]);
  return btoa(raw);
}

function plainNumber(value) {
  if (!Number.isFinite(value)) throw new Error("NON_FINITE_BINANCE_PARAM");
  if (Number.isInteger(value)) return String(value);
  const raw = String(value);
  if (!/[eE]/.test(raw)) return raw;
  const fixed = value.toFixed(20).replace(/0+$/, "").replace(/\.$/, "");
  if (!fixed || fixed === "-0") return "0";
  return fixed;
}

export function serializeBinanceValue(value) {
  if (typeof value === "number") return plainNumber(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

export function binanceCredentials(env = {}) {
  const apiKey = String(env.BINANCE_API_KEY || env.BINANCE_KEY || env.BINANCE_APIKEY || "").trim();
  const hmacSecret = String(env.BINANCE_API_SECRET || env.BINANCE_SECRET || env.BINANCE_SECRET_KEY || "").trim();
  const ed25519PrivateKey = String(env.BINANCE_ED25519_PRIVATE_KEY || "").trim();
  const requested = String(env.BINANCE_SIGNING_MODE || "HMAC").trim().toUpperCase();
  const signingMode = requested === "ED25519" ? "ED25519" : "HMAC";
  return { apiKey, hmacSecret, ed25519PrivateKey, signingMode };
}

export function signingCredentialsReady(credentials = {}) {
  if (!credentials.apiKey) return false;
  if (credentials.signingMode === "ED25519") return Boolean(credentials.ed25519PrivateKey);
  return Boolean(credentials.hmacSecret);
}

export function canonicalBinancePayload(params = {}, {
  timestampMs,
  recvWindow = 5000,
} = {}) {
  const ts = Number(timestampMs);
  const rw = Number(recvWindow);
  if (!Number.isFinite(ts) || ts <= 0) throw new Error("BAD_BINANCE_TIMESTAMP");
  if (!Number.isFinite(rw) || rw <= 0 || rw > 60000) throw new Error("BAD_BINANCE_RECV_WINDOW");

  const entries = [];
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    if (key === "signature" || key === "timestamp" || key === "recvWindow") continue;
    entries.push([String(key), serializeBinanceValue(value)]);
  }
  entries.push(["recvWindow", serializeBinanceValue(rw)]);
  entries.push(["timestamp", serializeBinanceValue(Math.trunc(ts))]);
  entries.sort(([a],[b]) => a.localeCompare(b));

  const q = new URLSearchParams();
  for (const [key, value] of entries) q.append(key, value);
  return q.toString();
}

export async function hmacSha256Hex(secret, payload) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(String(secret || "")),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(String(payload || "")));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function signEd25519Base64(privateKeyPem, payload) {
  const compact = pemBody(privateKeyPem, "PRIVATE KEY");
  if (!compact) throw new Error("ED25519_PRIVATE_KEY_EMPTY");
  const key = await crypto.subtle.importKey(
    "pkcs8",
    base64ToBytes(compact),
    { name: "Ed25519" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(String(payload || "")));
  return bytesToBase64(sig);
}

export async function signBinancePayload(credentials, payload) {
  if (!signingCredentialsReady(credentials)) throw new Error("BINANCE_SIGNING_CREDENTIALS_MISSING");
  if (credentials.signingMode === "ED25519") {
    return signEd25519Base64(credentials.ed25519PrivateKey, payload);
  }
  return hmacSha256Hex(credentials.hmacSecret, payload);
}

export async function buildSignedBinanceQuery(credentials, params = {}, options = {}) {
  const unsignedPayload = canonicalBinancePayload(params, options);
  const signature = await signBinancePayload(credentials, unsignedPayload);
  const query = unsignedPayload + "&signature=" + encodeURIComponent(signature);
  return {
    unsignedPayload,
    query,
    signature,
    timestampMs: Math.trunc(Number(options.timestampMs)),
    recvWindow: Number(options.recvWindow ?? 5000),
    signingMode: credentials.signingMode,
  };
}

export function computeServerTimeOffset({ localBeforeMs, localAfterMs, serverTimeMs }) {
  const before = Number(localBeforeMs), after = Number(localAfterMs), server = Number(serverTimeMs);
  if (![before, after, server].every(Number.isFinite) || after < before) throw new Error("BAD_SERVER_TIME_SAMPLE");
  const midpoint = before + (after - before) / 2;
  return {
    offsetMs: Math.round(server - midpoint),
    roundTripMs: Math.max(0, Math.round(after - before)),
    midpointMs: Math.round(midpoint),
  };
}

export function safeSigningDiagnostics({
  endpoint,
  method,
  unsignedPayload,
  timestampMs,
  serverTimeMs,
  httpStatus = null,
  binanceCode = null,
  signingMode = null,
} = {}) {
  const ts = Number(timestampMs);
  const st = Number(serverTimeMs);
  return {
    endpoint: String(endpoint || ""),
    method: String(method || "").toUpperCase(),
    unsignedCanonicalPayload: String(unsignedPayload || "").slice(0, 2000),
    timestampMs: Number.isFinite(ts) ? ts : null,
    serverTimeMs: Number.isFinite(st) ? st : null,
    timestampDeltaMs: Number.isFinite(ts) && Number.isFinite(st) ? ts - st : null,
    httpStatus: httpStatus == null ? null : Number(httpStatus),
    binanceCode: binanceCode == null ? null : Number(binanceCode),
    signingMode: signingMode ? String(signingMode) : null,
    secretExposed: false,
  };
}

export async function signRelayEnvelope(privateKeyPem, {
  relayTimestamp,
  relayNonce,
  method,
  path,
  apiKey,
  query,
} = {}) {
  const apiKeyHash = await sha256Hex(String(apiKey || ""));
  const queryHash = await sha256Hex(String(query || ""));
  const canonical = [
    "TST_SUPABASE_RELAY_V2",
    String(relayTimestamp || ""),
    String(relayNonce || ""),
    String(method || "").toUpperCase(),
    String(path || ""),
    apiKeyHash,
    queryHash,
  ].join("\n");
  return {
    relayCanonical: canonical,
    relaySignature: await signEd25519Base64(privateKeyPem, canonical),
  };
}

export async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(value || "")));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
