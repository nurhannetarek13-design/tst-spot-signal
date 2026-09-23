export default function handler(req,res) {
  res.setHeader("Cache-Control","no-store");
  return res.status(200).json({
    ok:true,
    makeBuyConfigured:Boolean(process.env.MAKE_ONE_TAP_WEBHOOK_URL),
    makeOcoConfigured:Boolean(process.env.MAKE_ONE_TAP_OCO_WEBHOOK_URL),
    telegramConfigured:Boolean(process.env.TELEGRAM_BOT_TOKEN),
    noSecretValuesExposed:true,
  });
}
