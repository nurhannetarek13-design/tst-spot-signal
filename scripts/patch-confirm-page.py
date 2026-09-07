from pathlib import Path
p=Path('src/buy-gateway.js')
s=p.read_text()

needle='function sanitizeBinanceError(text,status){try{const j=JSON.parse(text||"{}");return `${j.code||status}: ${j.msg||"Binance request failed"}`;}catch{return `HTTP ${status}`;}}\n'
insert='''function sanitizeBinanceError(text,status){try{const j=JSON.parse(text||"{}");return `${j.code||status}: ${j.msg||"Binance request failed"}`;}catch{return `HTTP ${status}`;}}\nfunction esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\\"":"&quot;","'":"&#39;"}[c]));}\nasync function confirmToken(env,id,expiresAt){return hmacHex(env.TELEGRAM_BOT_TOKEN||"",`${id}:${expiresAt}`);}\n'''
if needle in s:
    s=s.replace(needle,insert,1)

old='''    signal.recommendedUSDT=recommended;await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);\n    const baseAsset=p.symbol.endsWith("USDT")?p.symbol.slice(0,-4):p.symbol,url=`https://www.binance.com/en/trade/${encodeURIComponent(baseAsset)}_USDT?type=spot`;\n'''
new='''    signal.recommendedUSDT=recommended;await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);\n    const token=await confirmToken(env,id,signal.expiresAt);\n    const confirmUrl=`https://tst-spot-signal.nurhanne-tarek13.workers.dev/confirm?id=${encodeURIComponent(id)}&t=${encodeURIComponent(token)}`;\n    const baseAsset=p.symbol.endsWith("USDT")?p.symbol.slice(0,-4):p.symbol,url=`https://www.binance.com/en/trade/${encodeURIComponent(baseAsset)}_USDT?type=spot`;\n'''
if old in s:
    s=s.replace(old,new,1)

old='''    const rows=[];if(canBuy)rows.push([{text:`✅ BUY ${fmt(recommended)} USDT`,callback_data:`BUY:${id}`}]);rows.push([{text:"📈 افتح Binance Spot",url}]);\n'''
new='''    const rows=[];if(canBuy)rows.push([{text:`✅ REVIEW & BUY ${fmt(recommended)} USDT`,url:confirmUrl}]);rows.push([{text:"📈 افتح Binance Spot",url}]);\n'''
if old in s:
    s=s.replace(old,new,1)

marker='''async function handleTelegramWebhook(request,env){\n'''
block=r'''async function renderConfirmPage(url,env){
  const id=String(url.searchParams.get("id")||""),t=String(url.searchParams.get("t")||"");
  const signal=await getState(env,`live-signal:${id}`);
  if(!signal||Date.now()>Number(signal.expiresAt||0)) return new Response("Opportunity expired",{status:410,headers:{"content-type":"text/plain; charset=utf-8"}});
  const expected=await confirmToken(env,id,signal.expiresAt);
  if(t!==expected) return new Response("Invalid confirmation link",{status:403});
  const balance=await refreshBalance(env),free=Number(balance?.usdt?.free||0),recommended=dynamicQuote(free,Number(signal.entry),Number(signal.stop));
  const riskPct=((Number(signal.entry)-Number(signal.stop))/Number(signal.entry))*100;
  const profitPct=((Number(signal.target)-Number(signal.entry))/Number(signal.entry))*100;
  const disabled=recommended<MIN_ORDER_USDT?'disabled':'';
  const html=`<!doctype html><html lang="ar" dir="rtl"><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>Confirm Spot Buy</title><style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#0b0e11;color:#eaecef;margin:0;padding:20px}.card{max-width:520px;margin:auto;background:#181a20;border-radius:18px;padding:22px}.row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #2b3139}.buy{width:100%;margin-top:18px;padding:16px;border:0;border-radius:12px;font-weight:700;font-size:17px;background:#f0b90b;color:#111}.buy:disabled{opacity:.45}.note{font-size:13px;color:#848e9c;margin-top:12px}.title{font-size:22px;font-weight:800;margin-bottom:8px}</style></head><body><div class="card"><div class="title">${esc(signal.symbol.replace("USDT","/USDT"))} — Spot</div><div class="row"><span>المبلغ</span><b>${fmt(recommended)} USDT</b></div><div class="row"><span>Entry</span><b>${fmt(signal.entry)}</b></div><div class="row"><span>Take Profit</span><b>${fmt(signal.target)} (+${profitPct.toFixed(2)}%)</b></div><div class="row"><span>Stop Loss</span><b>${fmt(signal.stop)} (-${riskPct.toFixed(2)}%)</b></div><div class="row"><span>Score</span><b>${esc(signal.score||"—")}/100</b></div><div class="row"><span>USDT متاح</span><b>${fmt(free)}</b></div><form method="post" action="/confirm-buy"><input type="hidden" name="id" value="${esc(id)}"><input type="hidden" name="t" value="${esc(t)}"><button class="buy" ${disabled}>BUY ${fmt(recommended)} USDT</button></form><div class="note">لا يوجد شراء تلقائي. الأمر يُرسل إلى Binance فقط بعد ضغط زر BUY في هذه الصفحة. بعد الشراء يحاول النظام وضع TP/SL كـ OCO.</div></div></body></html>`;
  return new Response(html,{headers:{"content-type":"text/html; charset=utf-8","cache-control":"no-store"}});
}

async function handleConfirmBuy(request,env){
  const form=await request.formData(),id=String(form.get("id")||""),t=String(form.get("t")||"");
  const signal=await getState(env,`live-signal:${id}`);
  if(!signal||Date.now()>Number(signal.expiresAt||0)) return new Response("Opportunity expired",{status:410});
  const expected=await confirmToken(env,id,signal.expiresAt);
  if(t!==expected) return new Response("Invalid confirmation",{status:403});
  if(await getState(env,`live-used:${id}`)) return new Response("This opportunity was already used",{status:409});
  await putState(env,`live-used:${id}`,{startedAt:Date.now()},24*3600);
  try{
    const r=await executeDirect(env,signal);
    await tg(env,"sendMessage",{chat_id:String(env.TELEGRAM_CHAT_ID),text:[`✅ BUY تم — ${signal.symbol.replace("USDT","/USDT")}`,`💵 المصروف: ${fmt(r.quoteUSDT)} USDT`,`📦 الكمية: ${fmt(r.executedQty)}`,`💲 المتوسط: ${fmt(r.avg)}`,r.ocoPlaced?`✅ OCO: TP ${fmt(r.tp)} | SL ${fmt(r.stop)}`:`⚠️ الشراء تم لكن OCO فشل: ${r.ocoError||"unknown"}`].join("\n")});
    return new Response(`<!doctype html><html lang="ar" dir="rtl"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="font-family:sans-serif;background:#0b0e11;color:#eaecef;padding:28px"><h2>✅ تم الشراء</h2><p>${esc(signal.symbol.replace("USDT","/USDT"))}</p><p>تم استخدام ${fmt(r.quoteUSDT)} USDT</p><p>${r.ocoPlaced?`TP ${fmt(r.tp)} / SL ${fmt(r.stop)}`:"الشراء تم، لكن الحماية تحتاج مراجعة"}</p></body></html>`,{headers:{"content-type":"text/html; charset=utf-8","cache-control":"no-store"}});
  }catch(e){
    await putState(env,`live-used:${id}`,{failedAt:Date.now(),error:String(e?.message||e)},300);
    return new Response(`Execution failed: ${String(e?.message||e)}`,{status:500});
  }
}

'''
if marker in s and 'async function renderConfirmPage(' not in s:
    s=s.replace(marker,block+marker,1)

old='''    if(url.pathname==="/telegram-webhook"&&request.method==="POST") return handleTelegramWebhook(request,env);\n'''
new='''    if(url.pathname==="/confirm"&&request.method==="GET") return renderConfirmPage(url,env);\n    if(url.pathname==="/confirm-buy"&&request.method==="POST") return handleConfirmBuy(request,env);\n    if(url.pathname==="/telegram-webhook"&&request.method==="POST") return handleTelegramWebhook(request,env);\n'''
if old in s:
    s=s.replace(old,new,1)

p.write_text(s)
print('Prefilled manual confirmation page enabled; no order is sent until final BUY press')
