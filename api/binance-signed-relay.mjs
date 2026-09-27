export default async function handler(req, res) {
  res.setHeader("cache-control", "no-store");
  res.status(410).json({
    ok: false,
    status: "LEGACY_VERCEL_SIGNER_REVOKED",
    executionRoute: "CLOUDFLARE_HMAC_MAKE",
    fallbackAllowed: false,
    noSecretValuesExposed: true
  });
}
