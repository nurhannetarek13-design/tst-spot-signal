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
  big: {
    minVolume: 20_000_000,
    maxSpreadPct: 0.10,
    minDepth: 15_000,
    minRelVol: 1.35,
    minTaker: 0.55,
    minDepthRatio: 1.15,
    minScore: 92,
    maxPosition: 7,
    maxRisk: 0.20,
    maxStopPct: 2.5,
  },
  small: {
    minVolume: 5_000_000,
    maxVolume: 150_000_000,
    maxSpreadPct: 0.15,
    minDepth: 5_000,
    minRelVol: 1.40,
    minTaker: 0.57,
    minDepthRatio: 1.20,
    minScore: 94,
    maxPosition: 5.5,
    maxRisk: 0.10,
    maxStopPct: 2.2,
  },
};

const EXCLUDED = new Set([
  "USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL","GBP","AUD","USD1","RLUSD","USDE",
]);
const MAJORS = new Set(["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","LTC","BCH","LINK","AVAX","DOT"]);
const SAFE_B_SUFFIX = new Set(["BNB","ARB","KUB","WBB"]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/scanner-status") {
      const daily = await getDaily(env);
      const active = await getState(env, "paper:active") || [];
      return json({
        ok: true,
        mode: "SIGNAL_ONLY",
        liveTrading: false,
        cadence: "EVERY_MINUTE",
        lanes: ["LARGE_CAP","SMALL_CAP"],
        deepScanPerRun: CFG.scanPerRun,
        openPaperPositions: active.length,
        dailyRealizedPnlUSDT: round(daily.realizedPnlUSDT || 0, 4),
      });
    }
    if (url.pathname === "/paper-status") {
      return paperStatus(env);
    }
    if (url.pathname === "/scan-preview") {
      return json(await scan(env, false));
    }
    if (url.searchParams.get("test") === "telegram") {
      await telegram(env, "🧪 TEST — البوت الجديد شغال. دي مش إشارة شراء.");
      return json({ ok: true, telegramTest: "sent", mode: "SIGNAL_ONLY", liveTrading: false });
    }
    return json(await scan(env, true));
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      await monitorPaper(env);
      await scan(env, true);
    })());
  },
};

export class SignalState {
  constructor(ctx) { this.ctx = ctx; }
  async fetch(request) {
    const url = new URL(request.url);
    const key = url.searchParams.get("key") || "";
    if (!key || key.length > 300) return new Response("bad key", { status: 400 });
    if (request.method === "GET") {
      const row = await this.ctx.storage.get(key);
      if (!row) return Response.json(null);
      if (row.expiresAt && Date.now() >= row.expiresAt) {
        await this.ctx.storage.delete(key);
        return Response.json(null);
      }
      return Response.json(row.value);
    }
    if (request.method === "PUT") {
      const row = await request.json();
      await this.ctx.storage.put(key, row);
      return Response.json({ ok: true });
    }
    return new Response("not found", { status: 404 });
  }
}

async function scan(env, sendAlert) {
  try {
    const daily = await getDaily(env);
    if (Number(daily.realizedPnlUSDT || 0) <= -CFG.dailyLossCap) {
      return { ok: true, status: "DAILY_LOSS_CAP", liveTrading: false };
    }
    const active = (await getState(env, "paper:active") || []).filter(Boolean);
    if (active.length >= CFG.maxOpenPaper) {
      return { ok: true, status: "PAPER_POSITION_OPEN", symbol: active[0]?.symbol, liveTrading: false };
    }

    const [tickers, books, info, btc1hRaw, btc4hRaw] = await Promise.all([
      binance("/api/v3/ticker/24hr"),
      binance("/api/v3/ticker/bookTicker"),
      binance("/api/v3/exchangeInfo"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=120"),
      binance("/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=120"),
    ]);

    const btc1h = closed(btc1hRaw.map(candle));
    const btc4h = closed(btc4hRaw.map(candle));
    const regime = btcRegime(btc1h, btc4h);
    if (!regime.longAllowed) return { ok: true, status: "MARKET_RISK_OFF", marketRegime: regime, liveTrading: false };

    const tradable = new Map(info.symbols
      .filter(s => s.status === "TRADING" && s.quoteAsset === "USDT" && s.isSpotTradingAllowed)
      .map(s => [s.symbol, s]));
    const bookMap = new Map(books.map(x => [x.symbol, x]));
    const summaries = tickers.map(t => summarize(t, tradable.get(t.symbol), bookMap.get(t.symbol))).filter(Boolean);

    const bigPool = summaries.filter(x => x.volume >= CFG.big.minVolume)
      .sort((a,b) => opportunityRank(b) - opportunityRank(a));
    const smallPool = summaries.filter(x => !MAJORS.has(x.base) && x.volume >= CFG.small.minVolume && x.volume <= CFG.small.maxVolume)
      .sort((a,b) => opportunityRank(b) - opportunityRank(a));

    const selected = await rotateSelection(env, bigPool, smallPool);
    const analyses = [];
    for (let i = 0; i < selected.length; i += 3) {
      const group = selected.slice(i, i + 3);
      analyses.push(...await Promise.all(group.map(x => analyze(x, tradable.get(x.symbol), regime))));
    }
    const valid = analyses.filter(x => x.valid).sort((a,b) => b.score - a.score || b.edge - a.edge);
    const best = valid[0] || null;
    if (!best) {
      return {
        ok: true,
        status: "NO_STRONG_SETUP",
        checked: selected.length,
        candidates: analyses.slice().sort((a,b) => (b.score||0)-(a.score||0)).slice(0,3).map(x => ({ symbol:x.symbol, score:x.score||0, status:x.status })),
        liveTrading: false,
      };
    }

    const dedupeKey = `signal:${best.symbol}:${best.setup}:${best.signalBar}`;
    if (await getState(env, dedupeKey)) return { ok: true, status: "DUPLICATE_SUPPRESSED", symbol: best.symbol, liveTrading: false };

    const position = {
      symbol: best.symbol,
      lane: best.lane,
      setup: best.setup,
      entry: best.entry,
      stop: best.stop,
      target: best.target,
      quantity: best.quantity,
      notional: best.notional,
      score: best.score,
      openedAt: Date.now(),
      signalBar: best.signalBar,
    };
    await putState(env, dedupeKey, { createdAt: Date.now() }, CFG.duplicateHours * 3600);
    await putState(env, "paper:active", [position], CFG.maxHoldHours * 3600 + 7200);

    if (sendAlert) {
      const pair = best.symbol.replace("USDT", "/USDT");
      await telegram(env, [
        `🟢 PAPER BUY — ${pair} — SPOT`,
        `⭐ القوة: ${best.score}/100`,
        `💵 المبلغ: ${fmt(best.notional)} USDT`,
        `💲 دخول: ${fmt(best.entry)}`,
        `🛑 Stop: ${fmt(best.stop)}`,
        `🎯 Target: ${fmt(best.target)}`,
        `📦 الكمية: ${fmt(best.quantity)}`,
        "",
        "البوت هيتابع الصفقة ويبعث نتيجة الإغلاق تلقائيًا.",
        "Signal-only: مفيش شراء تلقائي من Binance.",
      ].join("\n"), {
        inline_keyboard: [[{ text: `🚀 افتحي ${pair} Spot`, url: `https://www.binance.com/en/trade/${best.symbol.slice(0,-4)}_USDT?type=spot` }]],
      });
    }
    return { ok:true, status:"PAPER_SIGNAL_SENT", signal:best, liveTrading:false };
  } catch (error) {
    return { ok:false, status:"SCAN_ERROR", error:String(error?.message || error), liveTrading:false };
  }
}

function summarize(t, info, book) {
  if (!info || !book) return null;
  const symbol = String(t.symbol || "");
  if (!symbol.endsWith("USDT")) return null;
  const base = symbol.slice(0,-4);
  if (!allowedBase(base)) return null;
  const bid = Number(book.bidPrice), ask = Number(book.askPrice);
  if (!(bid > 0 && ask > bid)) return null;
  const mid = (bid + ask)/2;
  const spreadPct = ((ask-bid)/mid)*100;
  return {
    symbol, base, bid, ask, spreadPct,
    volume: Number(t.quoteVolume || 0),
    change: Number(t.priceChangePercent || 0),
    trades: Number(t.count || 0),
  };
}

function allowedBase(base) {
  if (!base || EXCLUDED.has(base)) return false;
  if (/(UP|DOWN|BULL|BEAR)$/.test(base)) return false;
  if (base.endsWith("B") && !SAFE_B_SUFFIX.has(base)) return false;
  return true;
}

function opportunityRank(x) {
  const vol = Math.log10(Math.max(1, x.volume));
  const momentum = Math.max(-5, Math.min(5, x.change));
  const spreadPenalty = x.spreadPct * 20;
  return vol * 2 + momentum - spreadPenalty;
}

async function rotateSelection(env, bigPool, smallPool) {
  const state = await getState(env, "scan:rotation") || { big:0, small:0 };
  const result = [];
  const addFrom = (pool, cursor, count) => {
    if (!pool.length) return { cursor:0 };
    for (let i=0;i<Math.min(count,pool.length);i++) {
      const x = pool[(cursor+i)%pool.length];
      if (!result.some(r => r.symbol === x.symbol)) result.push(x);
    }
    return { cursor:(cursor+count)%pool.length };
  };
  const b = addFrom(bigPool, Number(state.big||0), 4);
  const s = addFrom(smallPool, Number(state.small||0), 4);
  await putState(env, "scan:rotation", { big:b.cursor, small:s.cursor, updatedAt:Date.now() }, 7*24*3600);
  return result.slice(0, CFG.scanPerRun);
}

async function analyze(summary, symbolInfo, regime) {
  const lane = MAJORS.has(summary.base) || summary.volume > CFG.small.maxVolume ? "LARGE_CAP" : "SMALL_CAP";
  const cfg = lane === "LARGE_CAP" ? CFG.big : CFG.small;
  try {
    const [raw15, raw1h, raw4h, depth] = await Promise.all([
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=15m&limit=140`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=1h&limit=120`),
      binance(`/api/v3/klines?symbol=${summary.symbol}&interval=4h&limit=120`),
      binance(`/api/v3/depth?symbol=${summary.symbol}&limit=100`),
    ]);
    const c15 = closed(raw15.map(candle)), h1 = closed(raw1h.map(candle)), h4 = closed(raw4h.map(candle));
    if (c15.length < 80 || h1.length < 80 || h4.length < 80) return { symbol:summary.symbol, valid:false, status:"HISTORY" };

    const closes15 = c15.map(x=>x.close), closes1 = h1.map(x=>x.close), closes4 = h4.map(x=>x.close);
    const e20_15 = ema(closes15,20), e50_15 = ema(closes15,50);
    const e20_1 = ema(closes1,20), e50_1 = ema(closes1,50);
    const e20_4 = ema(closes4,20), e50_4 = ema(closes4,50);
    const trend15 = closes15.at(-1) > e20_15 && e20_15 > e50_15;
    const trend1 = closes1.at(-1) > e20_1 && e20_1 > e50_1;
    const trend4 = closes4.at(-1) > e20_4 && e20_4 > e50_4;
    const setup = detectSetup(c15, e20_15);
    const last = c15.at(-1);
    const relBase = median(c15.slice(-25,-1).map(x=>x.quoteVolume));
    const relVol = relBase > 0 ? last.quoteVolume / relBase : 0;
    const flow = c15.slice(-8);
    const totalQ = flow.reduce((s,x)=>s+x.quoteVolume,0);
    const taker = totalQ > 0 ? flow.reduce((s,x)=>s+x.takerBuyQuote,0)/totalQ : 0;
    const rsi = rsi14(closes15);
    const atr = atr14(c15);
    const depthStats = orderBookStats(depth, summary.ask);
    const entry = summary.ask;
    const swing = setup?.swingLow || Math.min(...c15.slice(-4).map(x=>x.low));
    const stopRaw = Math.min(swing*0.998, entry-1.05*atr);
    const stop = roundPrice(stopRaw, entry);
    const riskUnit = entry-stop;
    const stopPct = riskUnit>0 ? riskUnit/entry*100 : 999;
    const filters = symbolInfo.filters || [];
    const lot = filters.find(f=>f.filterType==="LOT_SIZE");
    const notionalFilter = filters.find(f=>f.filterType==="NOTIONAL" || f.filterType==="MIN_NOTIONAL");
    const step = Number(lot?.stepSize || "0.00000001");
    const minNotional = Number(notionalFilter?.minNotional || 5);
    const qty = floorStep(Math.min(cfg.maxPosition/entry, riskUnit>0 ? cfg.maxRisk/riskUnit : 0), step);
    const notional = qty*entry;
    const stopNotional = qty*stop;
    const feeRisk = qty*riskUnit + notional*CFG.fee + stopNotional*CFG.fee;
    const target = roundPrice(entry + 3.2*riskUnit, entry);
    const targetNotional = qty*target;
    const rewardNet = qty*(target-entry) - notional*CFG.fee - targetNotional*CFG.fee;
    const rr = feeRisk>0 ? rewardNet/feeRisk : 0;

    const scoreParts = {
      regime: regime.longAllowed ? 8 : 0,
      trend15: trend15 ? 10 : 0,
      trend1: trend1 ? 14 : 0,
      trend4: trend4 ? 14 : 0,
      setup: setup ? 18 : 0,
      relVol: relVol >= cfg.minRelVol ? 10 : 0,
      taker: taker >= cfg.minTaker ? 10 : 0,
      depth: depthStats.bidAskRatio >= cfg.minDepthRatio ? 6 : 0,
      spread: summary.spreadPct <= cfg.maxSpreadPct ? 4 : 0,
      momentum: rsi >= 52 && rsi <= 67 ? 4 : 0,
      notExtended: summary.change <= 7 ? 2 : 0,
    };
    const score = Object.values(scoreParts).reduce((a,b)=>a+b,0);
    const hard = regime.longAllowed && trend15 && trend1 && trend4 && Boolean(setup)
      && summary.spreadPct <= cfg.maxSpreadPct
      && depthStats.bid >= cfg.minDepth && depthStats.ask >= cfg.minDepth
      && depthStats.bidAskRatio >= cfg.minDepthRatio
      && relVol >= cfg.minRelVol && taker >= cfg.minTaker
      && rsi >= 52 && rsi <= 67
      && stopPct > 0 && stopPct <= cfg.maxStopPct
      && notional >= minNotional*1.01 && stopNotional >= minNotional*1.01 && targetNotional >= minNotional*1.01
      && feeRisk <= cfg.maxRisk + 1e-8 && rr >= 2.2
      && score >= cfg.minScore;

    return {
      symbol:summary.symbol, lane, valid:hard, status:hard?"READY":"WAIT",
      score, scoreParts, setup:setup?.type || null, signalBar:setup?.time || last.openTime,
      entry, stop, target, quantity:qty, notional:round(notional,4),
      riskUSDT:round(feeRisk,4), netRR:round(rr,2), edge:round(relVol*taker*depthStats.bidAskRatio,3),
      metrics:{ relVol:round(relVol,2), taker:round(taker,3), rsi:round(rsi,1), spreadPct:round(summary.spreadPct,4), depthRatio:round(depthStats.bidAskRatio,2) },
    };
  } catch (error) {
    return { symbol:summary.symbol, lane, valid:false, status:"DATA_ERROR", error:String(error?.message||error) };
  }
}

function detectSetup(c, e20) {
  const last = c.at(-1);
  for (let i=Math.max(22,c.length-5); i<c.length-1; i++) {
    const prior = c.slice(i-20,i);
    const resistance = Math.max(...prior.map(x=>x.high));
    const vmed = median(prior.map(x=>x.quoteVolume));
    const b = c[i];
    if (b.close > resistance*1.001 && b.quoteVolume >= vmed*1.25) {
      const after = c.slice(i+1);
      const held = after.every(x=>x.close >= resistance*0.997);
      const retested = after.some(x=>x.low <= resistance*1.004);
      if (held && retested && last.close >= resistance) {
        return { type:"BREAKOUT_RETEST", time:b.openTime, level:resistance, swingLow:Math.min(...after.map(x=>x.low), b.low) };
      }
    }
  }
  const pull = c.slice(-3);
  const touched = pull.some(x=>x.low <= e20*1.003 && x.low >= e20*0.985);
  const recovered = last.close > e20 && last.close > last.open && last.close >= c.at(-2).close*0.998;
  if (touched && recovered) return { type:"TREND_PULLBACK", time:last.openTime, level:e20, swingLow:Math.min(...pull.map(x=>x.low)) };
  return null;
}

function btcRegime(h1,h4) {
  const c1=h1.map(x=>x.close), c4=h4.map(x=>x.close);
  const e20_1=ema(c1,20), e50_1=ema(c1,50), e20_4=ema(c4,20), e50_4=ema(c4,50);
  const longAllowed = c1.at(-1)>e50_1 && c4.at(-1)>e50_4 && e20_1>=e50_1*0.995 && e20_4>=e50_4*0.99;
  return { longAllowed, state:longAllowed?"LONGS_ALLOWED":"RISK_OFF", close1h:fmt(c1.at(-1)), close4h:fmt(c4.at(-1)), ema50_1h:fmt(e50_1), ema50_4h:fmt(e50_4) };
}

async function monitorPaper(env) {
  const active = (await getState(env,"paper:active") || []).filter(Boolean);
  if (!active.length) return;
  const keep=[];
  for (const p of active) {
    try {
      const priceData = await binance(`/api/v3/ticker/price?symbol=${p.symbol}`);
      const price = Number(priceData.price);
      const ageHours = (Date.now()-p.openedAt)/3600000;
      let reason=null, exit=price;
      if (price <= p.stop) { reason="STOP"; exit=price; }
      else if (price >= p.target) { reason="TARGET"; exit=price; }
      else if (ageHours >= CFG.maxHoldHours) { reason="TIME"; exit=price; }
      if (!reason) { keep.push(p); continue; }
      const gross = p.quantity*(exit-p.entry);
      const fees = p.notional*CFG.fee + p.quantity*exit*CFG.fee;
      const pnl = gross-fees;
      const daily = await getDaily(env);
      daily.realizedPnlUSDT = Number(daily.realizedPnlUSDT||0)+pnl;
      daily.trades = Number(daily.trades||0)+1;
      daily.wins = Number(daily.wins||0)+(pnl>0?1:0);
      await putState(env, dailyKey(), daily, 3*24*3600);
      await telegram(env, [
        `${pnl>=0?"✅":"🔴"} PAPER CLOSE — ${p.symbol.replace("USDT","/USDT")}`,
        `السبب: ${reason}`,
        `الدخول: ${fmt(p.entry)} | الخروج: ${fmt(exit)}`,
        `💰 النتيجة: ${pnl>=0?"+":""}${fmt(pnl)} USDT`,
        `📊 حصيلة اليوم: ${daily.realizedPnlUSDT>=0?"+":""}${fmt(daily.realizedPnlUSDT)} USDT`,
      ].join("\n"));
    } catch { keep.push(p); }
  }
  await putState(env,"paper:active",keep,CFG.maxHoldHours*3600+7200);
}

async function paperStatus(env) {
  const active = await getState(env,"paper:active") || [];
  const daily = await getDaily(env);
  return json({ ok:true, mode:"PAPER_ONLY", active, daily, liveTrading:false });
}

function candle(k) {
  return { openTime:Number(k[0]), open:Number(k[1]), high:Number(k[2]), low:Number(k[3]), close:Number(k[4]), volume:Number(k[5]), closeTime:Number(k[6]), quoteVolume:Number(k[7]), takerBuyQuote:Number(k[10]||0) };
}
function closed(c) { const now=Date.now(); return c.filter(x=>x.closeTime<now); }
function ema(values,p) { if (!values.length) return 0; const a=2/(p+1); let e=values[0]; for (let i=1;i<values.length;i++) e=values[i]*a+e*(1-a); return e; }
function rsi14(values) { if (values.length<15) return 50; let g=0,l=0; for (let i=values.length-14;i<values.length;i++){ const d=values[i]-values[i-1]; if(d>0)g+=d; else l-=d; } if(l===0)return 100; const rs=(g/14)/(l/14); return 100-100/(1+rs); }
function atr14(c) { if(c.length<15)return 0; const tr=[]; for(let i=c.length-14;i<c.length;i++){ const p=c[i-1].close,x=c[i]; tr.push(Math.max(x.high-x.low,Math.abs(x.high-p),Math.abs(x.low-p))); } return tr.reduce((a,b)=>a+b,0)/tr.length; }
function median(a) { if(!a.length)return 0; const x=[...a].sort((m,n)=>m-n); const h=Math.floor(x.length/2); return x.length%2?x[h]:(x[h-1]+x[h])/2; }
function orderBookStats(depth,mid) {
  const bids=(depth.bids||[]).map(([p,q])=>[Number(p),Number(q)]).filter(([p])=>p>=mid*0.99);
  const asks=(depth.asks||[]).map(([p,q])=>[Number(p),Number(q)]).filter(([p])=>p<=mid*1.01);
  const bid=bids.reduce((s,[p,q])=>s+p*q,0), ask=asks.reduce((s,[p,q])=>s+p*q,0);
  return { bid, ask, bidAskRatio:ask>0?bid/ask:0 };
}
function floorStep(v,step) { if(!(v>0&&step>0))return 0; const d=Math.max(0,(String(step).split(".")[1]||"").length); return Number((Math.floor((v+1e-12)/step)*step).toFixed(d)); }
function roundPrice(v,ref) { const d=ref>=1000?2:ref>=1?4:8; return Number(v.toFixed(d)); }
function round(v,d=2){ const f=10**d; return Math.round(Number(v)*f)/f; }
function fmt(v){ const n=Number(v); if(!Number.isFinite(n))return "-"; if(Math.abs(n)>=1000)return n.toFixed(2); if(Math.abs(n)>=1)return n.toFixed(4).replace(/0+$/,'').replace(/\.$/,''); return n.toFixed(8).replace(/0+$/,'').replace(/\.$/,''); }
function json(x,status=200){ return new Response(JSON.stringify(x),{status,headers:{"content-type":"application/json; charset=utf-8","cache-control":"no-store"}}); }

async function binance(path) {
  let last;
  for (const base of API_BASES) {
    try {
      const r=await fetch(base+path,{headers:{Accept:"application/json","User-Agent":"tst-edge-worker/1.0"},signal:AbortSignal.timeout(12000)});
      if(!r.ok) throw new Error(`${r.status}`);
      return await r.json();
    } catch(e){ last=e; }
  }
  throw last || new Error("Binance unavailable");
}

function stateStub(env){ const id=env.STATE_COORDINATOR.idFromName("global"); return env.STATE_COORDINATOR.get(id); }
async function getState(env,key){ const r=await stateStub(env).fetch(`https://state/get?key=${encodeURIComponent(key)}`); return r.ok?await r.json():null; }
async function putState(env,key,value,ttlSeconds){ const row={value,expiresAt:Date.now()+ttlSeconds*1000}; await stateStub(env).fetch(`https://state/put?key=${encodeURIComponent(key)}`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(row)}); }
function dailyKey(){ return `paper:daily:${new Date().toISOString().slice(0,10)}`; }
async function getDaily(env){ return await getState(env,dailyKey()) || { realizedPnlUSDT:0,trades:0,wins:0 }; }

async function telegram(env,text,replyMarkup=null) {
  if(!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return false;
  const body={chat_id:String(env.TELEGRAM_CHAT_ID),text,disable_web_page_preview:true};
  if(replyMarkup) body.reply_markup=replyMarkup;
  const r=await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body),signal:AbortSignal.timeout(10000)});
  return r.ok;
}
