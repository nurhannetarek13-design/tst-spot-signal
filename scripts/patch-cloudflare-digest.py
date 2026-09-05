from pathlib import Path

p = Path("src/edge-worker.js")
s = p.read_text()


def replace_once(old, new, label):
    global s
    if new in s:
        return
    if old not in s:
        raise SystemExit(f"{label} changed; refusing unsafe patch")
    s = s.replace(old, new, 1)


# 1) Keep the 5-minute Telegram digest on the Cloudflare scheduled worker.
replace_once(
    '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,true); })()); },',
    '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await sendPeriodicScanDigest(env); await scan(env,true); })()); },',
    "scheduled handler signature",
)

# 2) Surface the two reversal strategies in scanner status.
replace_once(
    'strategies:["TREND_BREAKOUT","MEAN_REVERSION","VOLATILITY_MOMENTUM","NEW_LISTING_MOMENTUM"]',
    'strategies:["TREND_BREAKOUT","MEAN_REVERSION","VOLATILITY_MOMENTUM","NEW_LISTING_MOMENTUM","LIQUIDITY_CRASH_EXHAUSTION","STOP_HUNT_REVERSAL"]',
    "strategy status list",
)

# 3) Do not abort the whole scan in BTC RISK_OFF. Normal strategies remain blocked
# later by the hard gate; only strict reversal setups may pass during RISK_OFF.
risk_off_abort = '    if(regime.state==="RISK_OFF") return {ok:true,status:"MARKET_RISK_OFF",marketRegime:regime,liveTrading:false};\n'
if risk_off_abort in s:
    s = s.replace(risk_off_abort, '', 1)

# 4) Reserve scan slots for liquid coins already in a sharp 24h selloff.
old_selection = '    const selected=await rotateSelection(env,focusPool,newPool);\n'
new_selection = '''    const crashPool=summaries
      .filter(x=>x.volume>=CFG.big.minVolume&&x.change<=-4&&x.spreadPct<=CFG.small.maxSpreadPct)
      .sort((a,b)=>a.change-b.change)
      .slice(0,3);
    const rotated=await rotateSelection(env,focusPool,newPool);
    const selected=[...crashPool,...rotated]
      .filter((x,i,a)=>a.findIndex(y=>y.symbol===x.symbol)===i)
      .slice(0,Math.max(CFG.scanPerRun,10));
'''
replace_once(old_selection, new_selection, "scan selection")

# 5) Add the two reversal detectors ahead of the existing regime strategies.
choose_marker = 'function chooseStrategy(regime,c,ctx){\n  const last=c.at(-1),prev=c.at(-2);'
reversal_code = r'''function detectStopHuntReversal(c,ctx){
  if(c.length<30) return null;
  const last=c.at(-1);
  const prior=c.slice(-22,-2);
  if(!prior.length) return null;
  const priorLow=Math.min(...prior.map(x=>x.low));
  const range=Math.max(last.high-last.low,1e-12);
  const lowerWick=Math.max(0,Math.min(last.open,last.close)-last.low);
  const swept=last.low<priorLow*0.997;
  const reclaimed=last.close>priorLow&&last.close>last.open&&((last.close-last.low)/range)>=0.60;
  const wickOk=(lowerWick/range)>=0.30;
  const relVolOk=ctx.relVol>=Math.max(ctx.cfg.minRelVol,1.35);
  const takerOk=ctx.taker>=Math.max(0.53,ctx.cfg.minTaker-0.02);
  const rsiOk=ctx.rsi<=48;
  if(!(swept&&reclaimed&&wickOk&&relVolOk&&takerOk&&rsiOk)) return null;
  return {
    strategy:"STOP_HUNT_REVERSAL",
    setup:"20_BAR_LOW_SWEEP_RECLAIM",
    signalBar:last.openTime,
    stop:last.low*0.996,
    rewardR:2.4,
    minRelVol:Math.max(ctx.cfg.minRelVol,1.35),
    minTaker:Math.max(0.53,ctx.cfg.minTaker-0.02),
    score:82
  };
}

function detectLiquidityCrashExhaustion(c,ctx){
  if(c.length<40) return null;
  const last=c.at(-1);
  const recent=c.slice(-6);
  const anchor=c.at(-7);
  const priorRanges=c.slice(-30,-6).map(x=>x.high-x.low).filter(x=>x>0);
  const normalRange=median(priorRanges);
  const maxRecentRange=Math.max(...recent.map(x=>x.high-x.low));
  const flushLow=Math.min(...recent.map(x=>x.low));
  const drop6=anchor?.close>0?(last.close/anchor.close)-1:0;
  const bounced=flushLow>0&&((last.close-flushLow)/flushLow)>=0.012;
  const capitulation=normalRange>0&&maxRecentRange>=normalRange*1.8;
  const bullishTurn=last.close>last.open;
  const relVolOk=ctx.relVol>=Math.max(ctx.cfg.minRelVol,1.60);
  const takerOk=ctx.taker>=Math.max(0.54,ctx.cfg.minTaker-0.01);
  const rsiOk=ctx.rsi<=42;
  if(!(drop6<=-0.045&&bounced&&capitulation&&bullishTurn&&relVolOk&&takerOk&&rsiOk)) return null;
  return {
    strategy:"LIQUIDITY_CRASH_EXHAUSTION",
    setup:"6_BAR_CAPITULATION_EXHAUSTION",
    signalBar:last.openTime,
    stop:flushLow*0.994,
    rewardR:2.8,
    minRelVol:Math.max(ctx.cfg.minRelVol,1.60),
    minTaker:Math.max(0.54,ctx.cfg.minTaker-0.01),
    score:84
  };
}

function chooseStrategy(regime,c,ctx){
  const last=c.at(-1),prev=c.at(-2);
  const stopHunt=detectStopHuntReversal(c,ctx);
  if(stopHunt) return stopHunt;
  const crashExhaustion=detectLiquidityCrashExhaustion(c,ctx);
  if(crashExhaustion) return crashExhaustion;'''
if 'function detectStopHuntReversal(c,ctx){' not in s:
    if choose_marker not in s:
        raise SystemExit("chooseStrategy signature changed; refusing unsafe patch")
    s = s.replace(choose_marker, reversal_code, 1)

# 6) Tighten the hard gate for reversal trades.
old_gate = '    const microOk=candidate.strategy==="MEAN_REVERSION"?microComposite>=-0.05:microComposite>=0.08;\n    const hard=marketRegime.state!=="RISK_OFF"&&summary.spreadPct<=cfg.maxSpreadPct&&depthStats.bid>=cfg.minDepth&&depthStats.ask>=cfg.minDepth&&depthStats.bidAskRatio>=cfg.minDepthRatio&&relVol>=candidate.minRelVol&&taker>=candidate.minTaker&&microOk&&stopPct>0&&stopPct<=cfg.maxStopPct&&notional>=minNotional*1.01&&stopNotional>=minNotional*1.01&&targetNotional>=minNotional*1.01&&feeRisk<=cfg.maxRisk+1e-8&&rr>=1.6&&score>=cfg.minScore;'
new_gate = '''    const reversalStrategy=candidate.strategy==="STOP_HUNT_REVERSAL"||candidate.strategy==="LIQUIDITY_CRASH_EXHAUSTION";
    const microOk=candidate.strategy==="MEAN_REVERSION"?microComposite>=-0.05:(reversalStrategy?microComposite>=0.12:microComposite>=0.08);
    const regimeOk=marketRegime.state!=="RISK_OFF"||reversalStrategy;
    const rrFloor=reversalStrategy?1.8:1.6;
    const scoreFloor=reversalStrategy?Math.max(cfg.minScore,93):cfg.minScore;
    const hard=regimeOk&&summary.spreadPct<=cfg.maxSpreadPct&&depthStats.bid>=cfg.minDepth&&depthStats.ask>=cfg.minDepth&&depthStats.bidAskRatio>=cfg.minDepthRatio&&relVol>=candidate.minRelVol&&taker>=candidate.minTaker&&microOk&&stopPct>0&&stopPct<=cfg.maxStopPct&&notional>=minNotional*1.01&&stopNotional>=minNotional*1.01&&targetNotional>=minNotional*1.01&&feeRisk<=cfg.maxRisk+1e-8&&rr>=rrFloor&&score>=scoreFloor;'''
if 'const reversalStrategy=candidate.strategy==="STOP_HUNT_REVERSAL"' not in s:
    if old_gate not in s:
        raise SystemExit("hard gate changed; refusing unsafe patch")
    s = s.replace(old_gate, new_gate, 1)

# 7) Add the read-only Telegram scan digest.
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

    const crashCandidates=summaries
      .filter(x=>x.change<=-4)
      .sort((a,b)=>a.change-b.change)
      .slice(0,3);
    const selected=[...crashCandidates,...summaries]
      .filter((x,i,a)=>a.findIndex(y=>y.symbol===x.symbol)===i)
      .slice(0,8);
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
      const strategy=x.strategy?` | ${x.strategy}`:"";
      const levels=(Number(x.entry)>0&&Number(x.stop)>0)?`\n   Entry ${fmt(x.entry)} | SL ${fmt(x.stop)}${Number(x.target)>0?` | TP ${fmt(x.target)}`:""}${Number(x.netRR)>0?` | R:R ${x.netRR}`:""}`:"";
      return `${i+1}) ${pair} | Score ${x.score||0}/100${price}${strategy}\n   ${state}${levels}`;
    });
    const msg=[
      "🔎 BINANCE SPOT SCAN — كل 5 دقايق",
      `🌦️ BTC regime: ${regime.state||"UNKNOWN"}`,
      `📊 ${selected.length} عملات اتفحصت بعمق — مع أولوية للـ crash/reversal`,
      "",
      ...(rows.length?rows:["مفيش candidates صالحة للتحليل دلوقتي."]),
      "",
      "⚠️ دي قائمة مرشحين للمراجعة، مش أمر BUY. لو Setup يعدّي التأكيدات هيوصل تنبيه منفصل."
    ].join("\n");
    const sent=await telegram(env,msg);
    if(sent) await putState(env,digestKey,{sentAt:Date.now()},600);
    return {ok:sent,status:sent?"DIGEST_SENT":"TELEGRAM_NOT_CONFIGURED",ranked:ranked.map(x=>({symbol:x.symbol,strategy:x.strategy,score:x.score||0,valid:!!x.valid,status:x.reason||x.status||null}))};
  }catch(error){
    return {ok:false,status:"DIGEST_ERROR",error:String(error?.message||error)};
  }
}

'''
if 'async function sendPeriodicScanDigest(env){' not in s:
    if marker not in s:
        raise SystemExit("scan function marker missing; refusing unsafe patch")
    s = s.replace(marker, digest + marker, 1)

# 8) Add a direct Binance Spot button to a strong alert.
signal_start = '      await telegram(env,[`🟡 LOCAL PAPER — ${pair} — SPOT`'
if 'const tradeButtons={inline_keyboard:' not in s:
    if signal_start not in s:
        raise SystemExit("strong signal Telegram call changed; refusing unsafe patch")
    button_prefix = '''      const baseAsset=best.symbol.endsWith("USDT")?best.symbol.slice(0,-4):best.symbol;
      const tradeUrl=`https://www.binance.com/en/trade/${encodeURIComponent(baseAsset)}_USDT?type=spot`;
      const tradeButtons={inline_keyboard:[[{text:"🟢 افتح الزوج على Binance Spot",url:tradeUrl}]]};
      await telegram(env,[`🟡 LOCAL PAPER — ${pair} — SPOT`'''
    s = s.replace(signal_start, button_prefix, 1)

signal_tail = '"Paper only — مفيش شراء حقيقي من Binance."].join("\\n"));'
patched_tail = '"📐 Net R:R: ${best.netRR||\\"-\\"}","🔗 Binance Spot: ${tradeUrl}","Paper only — مفيش شراء حقيقي من Binance."].join("\\n"),tradeButtons);'
if 'Net R:R: ${best.netRR' not in s:
    if signal_tail not in s:
        raise SystemExit("strong signal Telegram tail changed; refusing unsafe patch")
    s = s.replace(signal_tail, patched_tail, 1)

p.write_text(s)
