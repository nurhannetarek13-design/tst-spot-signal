export default async function handler(req,res) {
  res.setHeader("Cache-Control","no-store");
  let cloudflare={ok:false,status:"UNAVAILABLE"};
  try {
    const r=await fetch("https://tst-spot-signal.nurhanne-tarek13.workers.dev/direct-account-preflight?nonce="+Date.now(),{
      headers:{"cache-control":"no-store"},
      signal:AbortSignal.timeout(15000),
    });
    cloudflare=await r.json().catch(()=>({ok:false,status:"NON_JSON"}));
  } catch(e) {
    cloudflare={ok:false,status:"FETCH_FAILED"};
  }
  return res.status(200).json({
    ok:true,
    makeBuyConfigured:Boolean(process.env.MAKE_ONE_TAP_WEBHOOK_URL),
    makeOcoConfigured:Boolean(process.env.MAKE_ONE_TAP_OCO_WEBHOOK_URL),
    telegramConfigured:Boolean(process.env.TELEGRAM_BOT_TOKEN),
    cloudflareDirectPreflight:{
      ok:cloudflare?.ok===true,
      canTrade:cloudflare?.canTrade===true,
      signerMode:cloudflare?.signerMode||"UNKNOWN",
      diagnosticCode:cloudflare?.diagnosticCode||null,
      tradingAction:"NONE",
    },
    noSecretValuesExposed:true,
  });
}
