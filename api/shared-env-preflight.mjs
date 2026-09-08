export default function handler(req,res){
  res.status(200).setHeader('Cache-Control','no-store').json({
    telegramTokenPresent:Boolean((process.env.TELEGRAM_BOT_TOKEN||'').trim()),
    telegramChatPresent:Boolean((process.env.TELEGRAM_CHAT_ID||'').trim()),
    binanceKeyPresent:Boolean((process.env.BINANCE_API_KEY||'').trim()),
    binanceSecretPresent:Boolean((process.env.BINANCE_API_SECRET||'').trim())
  });
}
