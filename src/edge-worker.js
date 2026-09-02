const API_BASES = [
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
  "https://data-api.binance.vision",
];

const CFG = {
  capital: 20.08,
  fee: 0.001,
  dailyLossCap: 0.5,
  maxOpenPaper: 1,
  duplicateHours: 6,
  maxHoldHours: 36,
  scanPerRun: 8,
  big: { minVolume:20_000_000,maxSpreadPct:0.10,minDepth:15_000,minRelVol:1.25,minTaker:0.54,minDepthRatio:1.10,minScore:88,maxPosition:7,maxRisk:0.20,maxStopPct:2.5 },
  small:{ minVolume:5_000_000,maxVolume:150_000_000,maxSpreadPct:0.15,minDepth:5_000,minRelVol:1.30,minTaker:0.56,minDepthRatio:1.15,minScore:90,maxPosition:5.5,maxRisk:0.10,maxStopPct:2.2 },
};

const EXCLUDED = new Set(["USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE"]);
const MAJORS = new Set(["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"]);
const SAFE_B_SUFFIX = new Set(["BNB","ARB","KUB","WBB"]);
const FUSION_VALIDATORS = {
  freqtrade: "https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/validation/fusion/freqtrade-latest.json",
  jesse: "https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/validation/fusion/jesse-latest.json",
  vectorbt: "https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/validation/fusion/vectorbt-latest.json",
  nautilus: "https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/validation/fusion/nautilus-latest.json",
};
const FUSION_STRATEGY_ID = "TST_ADAPTIVE_FUSION_V1";
const EXPECTED_VALIDATOR_IDS = {
  freqtrade: "TST_ADAPTIVE_FUSION_V1",
  jesse: "TST_ADAPTIVE_FUSION_V1",
  vectorbt: "TST_DISCOVERY_VECTORBT_V1",
  nautilus: "TST_NAUTILUS_EXECUTION_VALIDATOR_V1",
};
const VALIDATOR_CACHE_SECONDS = 30 * 60;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/scanner-status") {
      const daily = await getDaily(env);
      const active = await getState(env,"paper:active") || [];
      const evidence = await getEvidence(env);
      const validators = await getFusionValidators(env,false); return json({ ok:true,mode:"FREE_FUSION_SHADOW",liveTrading:false,executorAllowed:false,cadence:"EVERY_MINUTE",strategies:["TREND_BREAKOUT","MEAN_REVERSION","VOLATILITY_MOMENTUM"],engines:["CLOUDFLARE_ORDERBOOK_ENGINE","VECTORBT_DISCOVERY","FREQTRADE_VALIDATOR","JESSE_VALIDATOR","NAUTILUS_EXECUTION_VALIDATOR"],openPaperPositions:active.length,dailyRealizedPnlUSDT:round(daily.realizedPnlUSDT||0,4),evidence:publicEvidence(evidence),validators });
    }
    if (url.pathname === "/paper-status") return paperStatus(env);
    if (url.pathname === "/fusion-status") {
      const validators = await getFusionValidators(env, url.searchParams.get("refresh") === "1");
      return json({ ok:true, mode:"FREE_FUSION_SHADOW", strategyId:FUSION_STRATEGY_ID, liveTrading:false, executorAllowed:false, validators, note:"Cloudflare runs the paper/order-book scanner. VectorBT discovers candidates; Freqtrade and Jesse validate; NautilusTrader stress-tests event-driven execution. No engine can authorize live trading." });
    }
    if (url.pathname === "/scan-preview") return json(await scan(env,false));
    if (url.searchParams.get("test") === "telegram") {
      await telegram(env,"🧪 TEST — Adaptive paper trader شغال. مفيش شراء حقيقي.");
      return json({ok:true,telegramTest:"sent",liveTrading:false});
    }
    return json(await scan(env,true));
  },
  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorPaper(env); await scan(env,true); })()); },
};

export class SignalState {
  constructor(ctx){ this.ctx=ctx; }
  async fetch(request){
    const url=new URL(request.url); const key=url.searchParams.get("key")||"";
    if(!key||key.length>300) return new Response("bad key",{status:400});
    if(request.method==="GET"){
      const row=await this.ctx.storage.get(key);
      if(!row) return Response.json(null);
      if(row.expiresAt&&Date.now()>=row.expiresAt){ await this.ctx.storage.delete(key); return Response.json(null); }
      return Response.json(row.value);
    }
    if(request.method==="PUT"){ const row=await request.json(); await this.ctx.storage.put(key,row); return Response.json({ok:true}); }
    return new Response("not found",{status:404});
  }
}

async function scan(env,sendAlert){
  try{
    const daily=await getDaily(env); const validators=await getFusionValidators(env,false);
    if(Number(daily.realizedPnlUSDT||0)<=-CFG.dailyLossCap) return {ok:true,status:"DAILY_LOSS_CAP",validators,liveTrading:false};
    const active=(await getState(env,"paper:active")||[]).filter(Boolean);
    if(active.length>=CFG.maxOpenPaper) return {ok:true,status:"PAPER_POSITION_OPEN",symbol:active[0]?.symbol,validators,liveTrading:false};

    const [tickers,books,info,btc1hRaw,btc4hRaw]=await Promise.all([
      binance("/api/v3/ticker/24hr"),binance("/api/v3/ticker/bookTicker"),binance("/api/v3/exchangeInfo"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=120"),binance("/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=120")
    ]);
    const regime=btcRegime(closed(btc1hRaw.map(candle)),closed(btc4hRaw.map(candle)));
    if(regime.state==="RISK_OFF") return {ok:true,status:"MARKET_RISK_OFF",marketRegime:regime,liveTrading:false};

    const tradable=new Map(info.symbols.filter(s=>s.status==="TRADING"&&s.quoteAsset==="USDT"&&s.isSpotTradingAllowed).map(s=>[s.symbol,s]));
    const bookMap=new Map(books.map(x=>[x.symbol,x]));
    const summaries=tickers.map(t=>summarize(t,tradable.get(t.symbol),bookMap.get(t.symbol))).filter(Boolean);
    const bigPool=summaries.filter(x=>x.volume>=CFG.big.minVolume).sort((a,b)=>opportunityRank(b)-opportunityRank(a));
    const smallPool=summaries.filter(x=>!MAJORS.has(x.base)&&x.volume>=CFG.small.minVolume&&x.volume<=CFG.small.maxVolume).sort((a,b)=>opportunityRank(b)-opportunityRank(a));
    const selected=await rotateSelection(env,bigPool,smallPool);
    const analyses=[];
    for(let i=0;i<selected.length;i+=3) analyses.push(...await Promise.all(selected.slice(i,i+3).map(x=>analyze(x,tradable.get(x.symbol),regime))));
    const valid=analyses.filter(x=>x.valid).sort((a,b)=>b.score-a.score||b.edge-a.edge);
    const best=valid[0]||null;
    if(!best) return {ok:true,status:"NO_STRONG_SETUP",marketRegime:regime,checked:selected.length,candidates:analyses.slice().sort((a,b)=>(b.score||0)-(a.score||0)).slice(0,3).map(x=>({symbol:x.symbol,strategy:x.strategy,score:x.score||0,status:x.status})),liveTrading:false};

    const dedupeKey=`signal:${best.symbol}:${best.strategy}:${best.signalBar}`;
    if(await getState(env,dedupeKey)) return {ok:true,status:"DUPLICATE_SUPPRESSED",symbol:best.symbol,liveTrading:false};
    const position={symbol:best.symbol,lane:best.lane,strategy:best.strategy,regime:best.regime,setup:best.setup,entry:best.entry,stop:best.stop,target:best.target,quantity:best.quantity,notional:best.notional,score:best.score,openedAt:Date.now(),signalBar:best.signalBar};
    await putState(env,dedupeKey,{createdAt:Date.now()},CFG.duplicateHours*3600);
    await putState(env,"paper:active",[position],CFG.maxHoldHours*3600+7200);

    if(sendAlert){
      const pair=best.symbol.replace("USDT","/USDT");
      const vline=`🧪 VBT: ${validators.vectorbt?.status||"PENDING"} | FT: ${validators.freqtrade?.status||"PENDING"} | Jesse: ${validators.jesse?.status||"PENDING"} | Nautilus: ${validators.nautilus?.status||"PENDING"}`; await telegram(env,[`🟢 PAPER BUY — ${pair} — SPOT`,`🧠 الاستراتيجية: ${best.strategy}`,`🌦️ السوق: ${best.regime}`,`⭐ القوة: ${best.score}/100`,`💵 المبلغ: ${fmt(best.notional)} USDT`,`💲 دخول: ${fmt(best.entry)}`,`🛑 Stop: ${fmt(best.stop)}`,`🎯 Target: ${fmt(best.target)}`,`📦 الكمية: ${fmt(best.quantity)}`,vline,"","Cloudflare للـpaper scan؛ VectorBT discovery؛ Freqtrade + Jesse validation؛ Nautilus execution stress.","Paper only — مفيش شراء حقيقي من Binance."].join("\n"));
    }
    return {ok:true,status:"PAPER_SIGNAL_SENT",signal:best,validators,liveTrading:false};
  }catch(error){ return {ok:false,status:"SCAN_ERROR",error:String(error?.message||error),liveTrading:false}; }
}

function summarize(t,info,book){
  if(!info||!book) return null;
  const symbol=String(t.symbol||""); if(!symbol.endsWith("USDT")) return null;
  const base=symbol.slice(0,-4); if(!allowedBase(base)) return null;
  const bid=Number(book.bidPrice),ask=Number(book.askPrice); if(!(bid>0&&ask>bid)) return null;
  const mid=(bid+ask)/2,spreadPct=((ask-bid)/mid)*100;
  return {symbol,base,bid,ask,spreadPct,volume:Number(t.quoteVolume||0),change:Number(t.priceChangePercent||0),trades:Number(t.count||0)};
}
function allowedBase(base){ if(!base||EXCLUDED.has(base)) return false; if(/(UP|DOWN|BULL|BEAR)$/.test(base)) return false; if(base.endsWith("B")&&!SAFE_B_SUFFIX.has(base)) return false; return true; }
function opportunityRank(x){ const vol=Math.log10(Math.max(1,x.volume)); const momentum=Math.max(-5,Math.min(5,x.change)); return vol*2+momentum-x.spreadPct*20; }

async function rotateSelection(env,bigPool,smallPool){
  const state=await getState(env,"scan:rotation")||{big:0,small:0}; const result=[];
  const addFrom=(pool,cursor,count)=>{ if(!pool.length) return {cursor:0}; for(let i=0;i<Math.min(count,pool.length);i++){ const x=pool[(cursor+i)%pool.length]; if(!result.some(r=>r.symbol===x.symbol)) result.push(x); } return {cursor:(cursor+count)%pool.length}; };
  const b=addFrom(bigPool,Number(state.big||0),4),s=addFrom(smallPool,Number(state.small||0),4);
  await putState(env,"scan:rotation",{big:b.cursor,small:s.cursor,updatedAt:Date.now()},7*24*3600); return result.slice(0,CFG.scanPerRun);
}

async function analyze(summary,symbolInfo,marketRegime){
  const lane=MAJORS.has(summary.base)||summary.volume>CFG.small.maxVolume?"LARGE_CAP":"SMALL_CAP"; const cfg=lane==="LARGE_CAP"?CFG.big:CFG.small;
  try{
    const [raw15,raw1h,raw4h,depth]=await Promise.all([
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=15m&limit=160`),binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1h&limit=140`),binance(`/api/v3/klines?symbol=${summary.symbol}&interval=4h&limit=120`),binance(`/api/v3/depth?symbol=${summary.symbol}&limit=100`)
    ]);
    const c15=closed(raw15.map(candle)),h1=closed(raw1h.map(candle)),h4=closed(raw4h.map(candle));
    if(c15.length<100||h1.length<100||h4.length<80) return {symbol:summary.symbol,valid:false,status:"HISTORY"};
    const closes15=c15.map(x=>x.close),closes1=h1.map(x=>x.close),closes4=h4.map(x=>x.close);
    const e20_15=ema(closes15,20),e50_15=ema(closes15,50),e20_1=ema(closes1,20),e50_1=ema(closes1,50),e20_4=ema(closes4,20),e50_4=ema(closes4,50);
    const atr=atr14(c15),atrPct=atr/closes15.at(-1)*100,last=c15.at(-1);
    const relBase=median(c15.slice(-25,-1).map(x=>x.quoteVolume)),relVol=relBase>0?last.quoteVolume/relBase:0;
    const flow=c15.slice(-8),totalQ=flow.reduce((s,x)=>s+x.quoteVolume,0),taker=totalQ>0?flow.reduce((s,x)=>s+x.takerBuyQuote,0)/totalQ:0;
    const rsi=rsi14(closes15),depthStats=orderBookStats(depth,summary.ask);
    const trendStrength=Math.abs(e20_1-e50_1)/e50_1*100;
    const symbolRegime = classifySymbolRegime({trendStrength,atrPct,relVol,rsi,e20_1,e50_1,e20_4,e50_4,lastClose:closes15.at(-1),e20_15,e50_15});
    const candidate = chooseStrategy(symbolRegime,c15,{e20_15,e50_15,e20_1,e50_1,e20_4,e50_4,atr,relVol,taker,rsi,summary,cfg,marketRegime});
    if(!candidate) return {symbol:summary.symbol,lane,valid:false,status:"NO_SETUP",strategy:null,regime:symbolRegime};

    const entry=summary.ask; const stopRaw=candidate.stop; const stop=roundPrice(stopRaw,entry); const riskUnit=entry-stop; const stopPct=riskUnit>0?riskUnit/entry*100:999;
    const filters=symbolInfo.filters||[],lot=filters.find(f=>f.filterType==="LOT_SIZE"),notionalFilter=filters.find(f=>f.filterType==="NOTIONAL"||f.filterType==="MIN_NOTIONAL");
    const step=Number(lot?.stepSize||"0.00000001"),minNotional=Number(notionalFilter?.minNotional||5);
    const qty=floorStep(Math.min(cfg.maxPosition/entry,riskUnit>0?cfg.maxRisk/riskUnit:0),step),notional=qty*entry,stopNotional=qty*stop;
    const feeRisk=qty*riskUnit+notional*CFG.fee+stopNotional*CFG.fee;
    const target=roundPrice(entry+candidate.rewardR*riskUnit,entry),targetNotional=qty*target,rewardNet=qty*(target-entry)-notional*CFG.fee-targetNotional*CFG.fee,rr=feeRisk>0?rewardNet/feeRisk:0;
    const baseScore=candidate.score;
    const liquidityScore=(summary.spreadPct<=cfg.maxSpreadPct?4:0)+(depthStats.bid>=cfg.minDepth&&depthStats.ask>=cfg.minDepth?4:0)+(depthStats.bidAskRatio>=cfg.minDepthRatio?4:0);
    const flowScore=(relVol>=candidate.minRelVol?5:0)+(taker>=candidate.minTaker?5:0);
    const score=Math.min(100,baseScore+liquidityScore+flowScore);
    const hard=marketRegime.state!=="RISK_OFF"&&summary.spreadPct<=cfg.maxSpreadPct&&depthStats.bid>=cfg.minDepth&&depthStats.ask>=cfg.minDepth&&depthStats.bidAskRatio>=cfg.minDepthRatio&&relVol>=candidate.minRelVol&&taker>=candidate.minTaker&&stopPct>0&&stopPct<=cfg.maxStopPct&&notional>=minNotional*1.01&&stopNotional>=minNotional*1.01&&targetNotional>=minNotional*1.01&&feeRisk<=cfg.maxRisk+1e-8&&rr>=1.6&&score>=cfg.minScore;
    return {symbol:summary.symbol,lane,valid:hard,status:hard?"READY":"WAIT",strategy:candidate.strategy,regime:symbolRegime,setup:candidate.setup,signalBar:candidate.signalBar,score,entry,stop,target,quantity:qty,notional:round(notional,4),riskUSDT:round(feeRisk,4),netRR:round(rr,2),edge:round(relVol*taker*depthStats.bidAskRatio,3),metrics:{relVol:round(relVol,2),taker:round(taker,3),rsi:round(rsi,1),atrPct:round(atrPct,2),spreadPct:round(summary.spreadPct,4),depthRatio:round(depthStats.bidAskRatio,2)}};
  }catch(error){ return {symbol:summary.symbol,lane,valid:false,status:"DATA_ERROR",error:String(error?.message||error)}; }
}

function classifySymbolRegime(x){
  const up=x.e20_1>x.e50_1&&x.e20_4>=x.e50_4&&x.lastClose>x.e20_15;
  if(up&&x.trendStrength>=0.35) return "TREND";
  if(x.atrPct>=1.0||x.relVol>=1.8) return "VOLATILITY_EXPANSION";
  return "RANGE";
}

function chooseStrategy(regime,c,ctx){
  const last=c.at(-1),prev=c.at(-2);
  if(regime==="TREND"){
    const s=detectTrendSetup(c,ctx.e20_15);
    if(!s) return null;
    return {strategy:"TREND_BREAKOUT",setup:s.type,signalBar:s.time,stop:Math.min(s.swingLow*0.998,ctx.summary.ask-1.05*ctx.atr),rewardR:3.0,minRelVol:ctx.cfg.minRelVol,minTaker:ctx.cfg.minTaker,score:72};
  }
  if(regime==="VOLATILITY_EXPANSION"){
    const hh=Math.max(...c.slice(-21,-1).map(x=>x.high));
    const breakout=last.close>hh*1.001&&last.close>last.open&&ctx.rsi>=55&&ctx.rsi<=72;
    if(!breakout) return null;
    const swing=Math.min(...c.slice(-4).map(x=>x.low));
    return {strategy:"VOLATILITY_MOMENTUM",setup:"20_BAR_BREAKOUT",signalBar:last.openTime,stop:Math.min(swing*0.998,ctx.summary.ask-1.15*ctx.atr),rewardR:2.6,minRelVol:Math.max(ctx.cfg.minRelVol,1.5),minTaker:Math.max(ctx.cfg.minTaker,0.56),score:74};
  }
  const mean=ema(c.map(x=>x.close),20),sd=std(c.slice(-20).map(x=>x.close));
  const lower=mean-1.8*sd;
  const washed=prev.low<=lower&&ctx.rsi<=38;
  const reclaim=last.close>prev.close&&last.close>last.open&&last.close>=lower;
  if(!(washed&&reclaim)) return null;
  const swing=Math.min(prev.low,last.low);
  return {strategy:"MEAN_REVERSION",setup:"LOWER_BAND_RECLAIM",signalBar:last.openTime,stop:Math.min(swing*0.997,ctx.summary.ask-0.9*ctx.atr),rewardR:2.0,minRelVol:1.0,minTaker:0.50,score:70};
}

function detectTrendSetup(c,e20){
  const last=c.at(-1);
  for(let i=Math.max(22,c.length-5);i<c.length-1;i++){
    const prior=c.slice(i-20,i),resistance=Math.max(...prior.map(x=>x.high)),vmed=median(prior.map(x=>x.quoteVolume)),b=c[i];
    if(b.close>resistance*1.001&&b.quoteVolume>=vmed*1.2){ const after=c.slice(i+1),held=after.every(x=>x.close>=resistance*0.997),retested=after.some(x=>x.low<=resistance*1.004); if(held&&retested&&last.close>=resistance) return {type:"BREAKOUT_RETEST",time:b.openTime,swingLow:Math.min(...after.map(x=>x.low),b.low)}; }
  }
  const pull=c.slice(-3),touched=pull.some(x=>x.low<=e20*1.003&&x.low>=e20*0.985),recovered=last.close>e20&&last.close>last.open&&last.close>=c.at(-2).close*0.998;
  if(touched&&recovered) return {type:"TREND_PULLBACK",time:last.openTime,swingLow:Math.min(...pull.map(x=>x.low))};
  return null;
}

function btcRegime(h1,h4){
  const c1=h1.map(x=>x.close),c4=h4.map(x=>x.close),e20_1=ema(c1,20),e50_1=ema(c1,50),e20_4=ema(c4,20),e50_4=ema(c4,50);
  const strong=c1.at(-1)>e50_1&&c4.at(-1)>e50_4&&e20_1>=e50_1&&e20_4>=e50_4;
  const neutral=c1.at(-1)>=e50_1*0.985&&c4.at(-1)>=e50_4*0.98;
  return {state:strong?"TREND_OK":neutral?"NEUTRAL":"RISK_OFF",close1h:fmt(c1.at(-1)),close4h:fmt(c4.at(-1)),ema50_1h:fmt(e50_1),ema50_4h:fmt(e50_4)};
}

async function monitorPaper(env){
  const active=(await getState(env,"paper:active")||[]).filter(Boolean); if(!active.length) return;
  const keep=[];
  for(const p of active){
    try{
      const priceData=await binance(`/api/v3/ticker/price?symbol=${p.symbol}`),price=Number(priceData.price),ageHours=(Date.now()-p.openedAt)/3600000;
      let reason=null,exit=price; if(price<=p.stop){reason="STOP";} else if(price>=p.target){reason="TARGET";} else if(ageHours>=CFG.maxHoldHours){reason="TIME";}
      if(!reason){keep.push(p);continue;}
      const gross=p.quantity*(exit-p.entry),fees=p.notional*CFG.fee+p.quantity*exit*CFG.fee,pnl=gross-fees;
      const daily=await getDaily(env); daily.realizedPnlUSDT=Number(daily.realizedPnlUSDT||0)+pnl; daily.trades=Number(daily.trades||0)+1; daily.wins=Number(daily.wins||0)+(pnl>0?1:0); await putState(env,dailyKey(),daily,3*24*3600);
      const evidence=await recordClosedTrade(env,{...p,exit,closedAt:Date.now(),reason,gross,fees,pnl});
      await telegram(env,[`${pnl>=0?"✅":"🔴"} PAPER CLOSE — ${p.symbol.replace("USDT","/USDT")}`,`🧠 ${p.strategy||"-"} | 🌦️ ${p.regime||"-"}`,`السبب: ${reason}`,`الدخول: ${fmt(p.entry)} | الخروج: ${fmt(exit)}`,`💰 النتيجة: ${pnl>=0?"+":""}${fmt(pnl)} USDT`,`📊 الإجمالي: ${evidence.netPnl>=0?"+":""}${fmt(evidence.netPnl)} USDT`,`📈 PF: ${fmt(evidence.profitFactor)} | الصفقات: ${evidence.closedTrades}`].join("\n"));
    }catch{ keep.push(p); }
  }
  await putState(env,"paper:active",keep,CFG.maxHoldHours*3600+7200);
}

async function recordClosedTrade(env,trade){
  const ledger=await getState(env,"paper:ledger")||[]; ledger.push(trade); if(ledger.length>500) ledger.splice(0,ledger.length-500); await putState(env,"paper:ledger",ledger,365*24*3600);
  const evidence=computeEvidence(ledger); await putState(env,"paper:evidence",evidence,365*24*3600); return evidence;
}
function computeEvidence(ledger){
  let netPnl=0,grossProfit=0,grossLoss=0,wins=0,equity=0,peak=0,maxDD=0;
  const profits=[]; const byStrategy={};
  for(const t of ledger){ const p=Number(t.pnl||0); netPnl+=p; equity+=p; peak=Math.max(peak,equity); maxDD=Math.max(maxDD,peak-equity); if(p>0){wins++;grossProfit+=p;profits.push(p);} else grossLoss+=Math.abs(p); const k=t.strategy||"UNKNOWN"; if(!byStrategy[k]) byStrategy[k]={trades:0,wins:0,netPnl:0}; byStrategy[k].trades++; byStrategy[k].wins+=p>0?1:0; byStrategy[k].netPnl+=p; }
  profits.sort((a,b)=>b-a); const top2=(profits[0]||0)+(profits[1]||0);
  return {closedTrades:ledger.length,wins,losses:ledger.length-wins,winRate:ledger.length?wins/ledger.length:0,netPnl,grossProfit,grossLoss,profitFactor:grossLoss>0?grossProfit/grossLoss:(grossProfit>0?999:0),expectancy:ledger.length?netPnl/ledger.length:0,maxDrawdownUSDT:maxDD,top2ProfitShare:grossProfit>0?top2/grossProfit:0,byStrategy,updatedAt:Date.now()};
}
async function getEvidence(env){ return await getState(env,"paper:evidence")||computeEvidence(await getState(env,"paper:ledger")||[]); }
function publicEvidence(e){ return {closedTrades:e.closedTrades,wins:e.wins,losses:e.losses,winRate:round((e.winRate||0)*100,1),netPnlUSDT:round(e.netPnl||0,4),profitFactor:round(e.profitFactor||0,3),expectancyUSDT:round(e.expectancy||0,4),maxDrawdownUSDT:round(e.maxDrawdownUSDT||0,4),top2ProfitShare:round(e.top2ProfitShare||0,3),byStrategy:e.byStrategy||{}}; }
async function paperStatus(env){ const active=await getState(env,"paper:active")||[],daily=await getDaily(env),evidence=await getEvidence(env),ledger=await getState(env,"paper:ledger")||[]; return json({ok:true,mode:"ADAPTIVE_MULTI_STRATEGY_PAPER",active,daily,evidence:publicEvidence(evidence),recentClosed:ledger.slice(-10).reverse().map(t=>({symbol:t.symbol,strategy:t.strategy,regime:t.regime,pnl:round(t.pnl,4),reason:t.reason,closedAt:t.closedAt})),liveTrading:false}); }

function candle(k){ return {openTime:Number(k[0]),open:Number(k[1]),high:Number(k[2]),low:Number(k[3]),close:Number(k[4]),volume:Number(k[5]),closeTime:Number(k[6]),quoteVolume:Number(k[7]),takerBuyQuote:Number(k[10]||0)}; }
function closed(c){const now=Date.now();return c.filter(x=>x.closeTime<now);} function ema(values,p){if(!values.length)return 0;const a=2/(p+1);let e=values[0];for(let i=1;i<values.length;i++)e=values[i]*a+e*(1-a);return e;}
function rsi14(values){if(values.length<15)return 50;let g=0,l=0;for(let i=values.length-14;i<values.length;i++){const d=values[i]-values[i-1];if(d>0)g+=d;else l-=d;}if(l===0)return 100;const rs=(g/14)/(l/14);return 100-100/(1+rs);} function atr14(c){if(c.length<15)return 0;const tr=[];for(let i=c.length-14;i<c.length;i++){const p=c[i-1].close,x=c[i];tr.push(Math.max(x.high-x.low,Math.abs(x.high-p),Math.abs(x.low-p)));}return tr.reduce((a,b)=>a+b,0)/tr.length;}
function median(a){if(!a.length)return 0;const x=[...a].sort((m,n)=>m-n),h=Math.floor(x.length/2);return x.length%2?x[h]:(x[h-1]+x[h])/2;} function std(a){if(!a.length)return 0;const m=a.reduce((s,x)=>s+x,0)/a.length;return Math.sqrt(a.reduce((s,x)=>s+(x-m)**2,0)/a.length);} function orderBookStats(depth,mid){const bids=(depth.bids||[]).map(([p,q])=>[Number(p),Number(q)]).filter(([p])=>p>=mid*0.99),asks=(depth.asks||[]).map(([p,q])=>[Number(p),Number(q)]).filter(([p])=>p<=mid*1.01);const bid=bids.reduce((s,[p,q])=>s+p*q,0),ask=asks.reduce((s,[p,q])=>s+p*q,0);return{bid,ask,bidAskRatio:ask>0?bid/ask:0};}
function floorStep(v,step){if(!(v>0&&step>0))return 0;const d=Math.max(0,(String(step).split(".")[1]||"").length);return Number((Math.floor((v+1e-12)/step)*step).toFixed(d));} function roundPrice(v,ref){const d=ref>=1000?2:ref>=1?4:8;return Number(v.toFixed(d));} function round(v,d=2){const f=10**d;return Math.round(Number(v)*f)/f;} function fmt(v){const n=Number(v);if(!Number.isFinite(n))return"-";if(Math.abs(n)>=1000)return n.toFixed(2);if(Math.abs(n)>=1)return n.toFixed(4).replace(/0+$/,'').replace(/\.$/,'');return n.toFixed(8).replace(/0+$/,'').replace(/\.$/,'');} function json(x,status=200){return new Response(JSON.stringify(x),{status,headers:{"content-type":"application/json; charset=utf-8","cache-control":"no-store"}});}

async function getFusionValidators(env,force=false){
  const cached=await getState(env,"fusion:validators");
  if(!force&&cached&&Number(cached.fetchedAt||0)>Date.now()-VALIDATOR_CACHE_SECONDS*1000) return cached;
  const entries=await Promise.all(Object.entries(FUSION_VALIDATORS).map(async([name,url])=>{
    try{
      const r=await fetch(url,{headers:{Accept:"application/json","User-Agent":"tst-fusion-worker/1.0"},signal:AbortSignal.timeout(8000),cf:{cacheTtl:300,cacheEverything:true}});
      if(!r.ok) throw new Error(String(r.status));
      const report=await r.json();
      const expected=EXPECTED_VALIDATOR_IDS[name]||FUSION_STRATEGY_ID;
      const same=report?.strategyId===expected;
      return [name,{...report,strategyMatch:same,usable:Boolean(same&&report?.generatedAt)}];
    }catch(error){
      return [name,{engine:name.toUpperCase(),status:"UNAVAILABLE",pass:false,strategyMatch:false,usable:false,error:String(error?.message||error)}];
    }
  }));
  const out=Object.fromEntries(entries); out.fetchedAt=Date.now();
  await putState(env,"fusion:validators",out,2*3600);
  return out;
}

async function binance(path){let last;for(const base of API_BASES){try{const r=await fetch(base+path,{headers:{Accept:"application/json","User-Agent":"tst-edge-worker/2.0"},signal:AbortSignal.timeout(12000)});if(!r.ok)throw new Error(`${r.status}`);return await r.json();}catch(e){last=e;}}throw last||new Error("Binance unavailable");}
function stateStub(env){const id=env.STATE_COORDINATOR.idFromName("global");return env.STATE_COORDINATOR.get(id);} async function getState(env,key){const r=await stateStub(env).fetch(`https://state/get?key=${encodeURIComponent(key)}`);return r.ok?await r.json():null;} async function putState(env,key,value,ttlSeconds){const row={value,expiresAt:Date.now()+ttlSeconds*1000};await stateStub(env).fetch(`https://state/put?key=${encodeURIComponent(key)}`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(row)});} function dailyKey(){return `paper:daily:${new Date().toISOString().slice(0,10)}`;} async function getDaily(env){return await getState(env,dailyKey())||{realizedPnlUSDT:0,trades:0,wins:0};}
async function telegram(env,text,replyMarkup=null){if(!env.TELEGRAM_BOT_TOKEN||!env.TELEGRAM_CHAT_ID)return false;const body={chat_id:String(env.TELEGRAM_CHAT_ID),text,disable_web_page_preview:true};if(replyMarkup)body.reply_markup=replyMarkup;const r=await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body),signal:AbortSignal.timeout(10000)});return r.ok;}
