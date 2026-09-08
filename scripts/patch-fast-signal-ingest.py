from pathlib import Path

p=Path('src/buy-gateway-stable.js')
s=p.read_text(encoding='utf-8')

helper='''\nasync function handleFastSignalIngest(request, env) {\n  const c = creds(env);\n  if (c.credentialMode !== "LIVE") {\n    return Response.json({ ok:false, status:"LIVE_CREDENTIALS_REQUIRED", credentialMode:c.credentialMode, autoBuy:false }, {status:503});\n  }\n  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {\n    return Response.json({ ok:false, status:"TELEGRAM_NOT_CONFIGURED", autoBuy:false }, {status:503});\n  }\n  const raw = await request.text();\n  const ts = String(request.headers.get("x-fast-timestamp") || "");\n  const supplied = String(request.headers.get("x-fast-signature") || "").toLowerCase();\n  const stamp = Number(ts);\n  if (!Number.isFinite(stamp) || Math.abs(Date.now() - stamp) > 60_000) {\n    return Response.json({ok:false,status:"STALE_INGEST"},{status:401});\n  }\n  const expected = await hmacHex(env.TELEGRAM_BOT_TOKEN, `${ts}.${raw}`);\n  if (!/^[a-f0-9]{64}$/.test(supplied) || supplied !== expected) {\n    return Response.json({ok:false,status:"BAD_INGEST_SIGNATURE"},{status:401});\n  }\n  let body={};\n  try { body=JSON.parse(raw || "{}"); } catch { return Response.json({ok:false,status:"BAD_JSON"},{status:400}); }\n  const symbol=String(body.symbol||"").toUpperCase();\n  const entry=Number(body.entry), stop=Number(body.stop), target=Number(body.target), requested=Number(body.stakeUSDT);\n  const score=Number(body.score);\n  if (!/^[A-Z0-9]{1,20}USDT$/.test(symbol)) return Response.json({ok:false,status:"BAD_SYMBOL"},{status:400});\n  if (![entry,stop,target].every(Number.isFinite) || !(stop < entry && target > entry)) return Response.json({ok:false,status:"BAD_LEVELS"},{status:400});\n  if (!Number.isFinite(requested) || requested < MIN_ORDER_USDT || requested > 10) return Response.json({ok:false,status:"BAD_STAKE"},{status:400});\n\n  const b=await refreshBalance(env);\n  if (!b?.ok || b.credentialMode !== "LIVE" || !b.canTrade) {\n    return Response.json({ok:false,status:"ACCOUNT_PREFLIGHT_FAILED",autoBuy:false},{status:503});\n  }\n  const free=Number(b.usdt?.free||0);\n  const rec=dynamicQuote(free,entry,stop,requested);\n  if (rec < MIN_ORDER_USDT) return Response.json({ok:false,status:"SIZE_TOO_SMALL",autoBuy:false},{status:409});\n  if (body.dryRun === true) {\n    return Response.json({ok:true,status:"FAST_SIGNAL_DRYRUN_OK",canTrade:true,credentialMode:"LIVE",autoBuy:false,userConfirmationRequired:true,recommendedUSDT:rec});\n  }\n  const rawId=String(body.id||`${symbol}-${Date.now()}`);\n  const id=rawId.replace(/[^A-Za-z0-9_-]/g,"").slice(0,40) || compactId({symbol,entry,createdAt:Date.now()});\n  const now=Date.now();\n  const signal={id,symbol,entry,stop,target,strategy:String(body.strategy||"FAST30_60").slice(0,100),score:Number.isFinite(score)?score:null,createdAt:now,expiresAt:now+SIGNAL_TTL_SEC*1000,recommendedUSDT:rec,confirmedQuoteUSDT:rec,prepareExpiresAt:now+PREPARE_TTL_SEC*1000};\n  await putState(env,`live-signal:${id}`,signal,SIGNAL_TTL_SEC);\n  await putState(env,`prepared:${id}`,signal,PREPARE_TTL_SEC);\n  await tg(env,"sendMessage",{\n    chat_id:String(env.TELEGRAM_CHAT_ID),\n    text:`🚨 CONFIRMED BUY — ${symbol} — SPOT\\n💵 ${fmt(rec)} USDT\\n💲 Entry ref ${fmt(entry)}\\n🎯 TP ${fmt(target)}\\n🛑 SL ${fmt(stop)}\\n⭐ Score ${Number.isFinite(score)?score:"—"}/100\\n\\n⚡ ضغطة CONFIRM BUY تنفذ Market Buy حقيقي ثم تحط TP/SL تلقائيًا.`,\n    reply_markup:{inline_keyboard:[[{text:`✅ CONFIRM BUY ${fmt(rec)} USDT`,callback_data:`CONFIRM:${id}`}],[{text:"❌ CANCEL",callback_data:`CANCEL:${id}`}]]}\n  });\n  await putState(env,`buy-prompt:${id}`,{sentAt:now,source:"FAST_INGEST"},SIGNAL_TTL_SEC);\n  return Response.json({ok:true,status:"FAST_SIGNAL_READY",id,symbol,recommendedUSDT:rec,canTrade:true,credentialMode:"LIVE",autoBuy:false,userConfirmationRequired:true});\n}\n'''

marker='async function tg(env, method, payload) {'
if 'async function handleFastSignalIngest(' not in s:
    if marker not in s: raise SystemExit('tg marker missing')
    s=s.replace(marker,helper+'\n'+marker,1)
else:
    old='''  if (rec < MIN_ORDER_USDT) return Response.json({ok:false,status:"SIZE_TOO_SMALL",autoBuy:false},{status:409});\n  const rawId=String(body.id||`${symbol}-${Date.now()}`);'''
    new='''  if (rec < MIN_ORDER_USDT) return Response.json({ok:false,status:"SIZE_TOO_SMALL",autoBuy:false},{status:409});\n  if (body.dryRun === true) {\n    return Response.json({ok:true,status:"FAST_SIGNAL_DRYRUN_OK",canTrade:true,credentialMode:"LIVE",autoBuy:false,userConfirmationRequired:true,recommendedUSDT:rec});\n  }\n  const rawId=String(body.id||`${symbol}-${Date.now()}`);'''
    if 'FAST_SIGNAL_DRYRUN_OK' not in s:
        if old not in s: raise SystemExit('dry-run insertion marker missing')
        s=s.replace(old,new,1)

route='''    if (url.pathname === "/fast-signal-ingest" && request.method === "POST") return handleFastSignalIngest(request, env);\n'''
route_marker='''    if (url.pathname === "/telegram-webhook" && request.method === "POST") return handleTelegramWebhook(request, env);\n'''
if route not in s:
    if route_marker not in s: raise SystemExit('fetch route marker missing')
    s=s.replace(route_marker,route+route_marker,1)

runtime_marker='''        atomicConfirmClaim: true,\n        demoExecutionDisabled: c.credentialMode === "DEMO",\n'''
runtime_new='''        atomicConfirmClaim: true,\n        fastSignalIngest: true,\n        oneTapConfirm: true,\n        demoExecutionDisabled: c.credentialMode === "DEMO",\n'''
if runtime_new not in s:
    if runtime_marker not in s: raise SystemExit('runtime marker missing')
    s=s.replace(runtime_marker,runtime_new,1)

for need in ['handleFastSignalIngest','/fast-signal-ingest','FAST_SIGNAL_READY','FAST_SIGNAL_DRYRUN_OK','CONFIRM:${id}','fastSignalIngest: true','autoBuy:false']:
    if need not in s: raise SystemExit(f'missing {need}')
p.write_text(s,encoding='utf-8')
print('patched fast signal ingest + dry-run + one-tap confirm')
