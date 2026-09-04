from pathlib import Path

p = Path("src/edge-worker.js")
s = p.read_text()

old = '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,true); })()); },'
new = '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await sendPeriodicScanDigest(env); await scan(env,true); })()); },'
if old not in s:
    raise SystemExit("scheduled handler signature changed; refusing unsafe patch")
s = s.replace(old, new, 1)

marker = 'async function scan(env,sendAlert){'
digest = r'''async function sendPeriodicScanDigest(env){
  try{
    const bucket=Math.floor(Date.now()/300000);
    const digestKey=`telegram:scan-digest:${bucket}`;
    if(await getState(env,digestKey)) return {ok:true,status:"DIGEST_ALREADY_SENT"};

    const [tickers,books,info,btc1hRaw,btc4hRaw]=await Promise.all([
      binance("/api/v3/ticker/24hr"),
      binance("/api/v3/ticker/bookTicker"),
      binance("/api/v3/exchangeInfo"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=120"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=120")
    ]);
    const regime=btcRegime(closed(btc1hRaw.map(candle)),closed(btc4hRaw.map(candle)));
    const tradable=new Map(info.symbols.filter(s=>s.status==="TRADING"&&s.quoteAsset==="USDT"&&s.isSpotTradingAllowed).map(s=>[s.symbol,s]));
    const bookMap=new Map(books.map(x=>[x.symbol,x]));
    const summaries=tickers
      .map(t=>summarize(t,tradable.get(t.symbol),bookMap.get(t.symbol)))
      .filter(x=>x&&x.volume>=20_000_000&&x.spreadPct<=0.15)
      .sort((a,b)=>opportunityRank(b)-opportunityRank(a));

    const selected=summaries.slice(0,8);
    const bySymbol=new Map(selected.map(x=>[x.symbol,x]));
    const analyses=[];
    for(let i=0;i<selected.length;i+=2){
      analyses.push(...await Promise.all(selected.slice(i,i+2).map(x=>analyze(x,tradable.get(x.symbol),regime))));
    }
    const ranked=analyses.slice().sort((a,b)=>(b.score||0)-(a.score||0)).slice(0,5);
    const rows=ranked.map((x,i)=>{
      const q=bySymbol.get(x.symbol)||{};
      const pair=String(x.symbol||"").replace("USDT","/USDT");
      const state=x.valid?"✅ READY":`⏳ ${x.reason||x.status||"WAIT"}`;
      const price=Number(q.ask)>0?` | الآن ${fmt(q.ask)}`:"";
      const levels=(Number(x.entry)>0&&Number(x.stop)>0)?`\n   Entry ${fmt(x.entry)} | SL ${fmt(x.stop)}${Number(x.target)>0?` | TP ${fmt(x.target)}`:""}`:"";
      return `${i+1}) ${pair} | Score ${x.score||0}/100${price}\n   ${state}${levels}`;
    });
    const msg=[
      "🔎 BINANCE SPOT SCAN — كل 5 دقايق",
      `🌦️ BTC regime: ${regime.state||"UNKNOWN"}`,
      `📊 ${selected.length} عملات اتفحصت بعمق من أعلى السيولة`,
      "",
      ...(rows.length?rows:["مفيش candidates صالحة للتحليل دلوقتي."]),
      "",
      "⚠️ دي قائمة مرشحين للمراجعة، مش أمر BUY. لو Setup يعدّي التأكيدات هيوصل تنبيه منفصل."
    ].join("\n");
    const sent=await telegram(env,msg);
    if(sent) await putState(env,digestKey,{sentAt:Date.now()},600);
    return {ok:sent,status:sent?"DIGEST_SENT":"TELEGRAM_NOT_CONFIGURED",ranked:ranked.map(x=>({symbol:x.symbol,score:x.score||0,valid:!!x.valid,status:x.reason||x.status||null}))};
  }catch(error){
    return {ok:false,status:"DIGEST_ERROR",error:String(error?.message||error)};
  }
}

'''
if marker not in s:
    raise SystemExit("scan function marker missing; refusing unsafe patch")
s = s.replace(marker, digest + marker, 1)

signal_start = '      await telegram(env,[`🟡 LOCAL PAPER — ${pair} — SPOT`'
if signal_start not in s:
    raise SystemExit("strong signal Telegram call changed; refusing unsafe patch")
button_prefix = '''      const baseAsset=best.symbol.endsWith("USDT")?best.symbol.slice(0,-4):best.symbol;
      const tradeUrl=`https://www.binance.com/en/trade/${encodeURIComponent(baseAsset)}_USDT?type=spot`;
      const tradeButtons={inline_keyboard:[[{text:"🟢 افتح الزوج على Binance Spot",url:tradeUrl}]]};
      await telegram(env,[`🟡 LOCAL PAPER — ${pair} — SPOT`'''
s = s.replace(signal_start, button_prefix, 1)

signal_tail = '"Paper only — مفيش شراء حقيقي من Binance."].join("\\n"));'
if signal_tail not in s:
    raise SystemExit("strong signal Telegram tail changed; refusing unsafe patch")
s = s.replace(signal_tail, '"🔗 Binance Spot: ${tradeUrl}","Paper only — مفيش شراء حقيقي من Binance."].join("\\n"),tradeButtons);', 1)

p.write_text(s)
