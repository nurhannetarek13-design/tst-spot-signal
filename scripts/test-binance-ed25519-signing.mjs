import assert from "node:assert/strict";
import { signEd25519Base64 } from "../src/supabase-live-client.js";

function toPem(arrayBuffer) {
  const b64 = Buffer.from(arrayBuffer).toString("base64");
  const lines = b64.match(/.{1,64}/g) || [];
  return "-----BEGIN PRIVATE KEY-----\n" + lines.join("\n") + "\n-----END PRIVATE KEY-----";
}

const pair = await crypto.subtle.generateKey(
  { name: "Ed25519" },
  true,
  ["sign", "verify"],
);

const pkcs8 = await crypto.subtle.exportKey("pkcs8", pair.privateKey);
const pem = toPem(pkcs8);
const payload = "symbol=BTCUSDT&recvWindow=5000&timestamp=1800000000000";
const signatureB64 = await signEd25519Base64(pem, payload);
const signature = Buffer.from(signatureB64, "base64");

assert.equal(signature.length, 64);
assert.match(signatureB64, /^[A-Za-z0-9+/]{86}==$/);
assert.equal(
  await crypto.subtle.verify(
    "Ed25519",
    pair.publicKey,
    signature,
    new TextEncoder().encode(payload),
  ),
  true,
);

console.log("BINANCE_ED25519_SIGNING_SELFTEST_PASS");
