import crypto from 'node:crypto';

const API_BASES = [
  'https://api.binance.com',
  'https://api-gcp.binance.com',
  'https://api1.binance.com',
  'https://api2.binance.com',
  'https://api3.binance.com',
  'https://api4.binance.com',
];
const API_KEY = (process.env.BINANCE_API_KEY || '').trim();
const API_SECRET = (process.env.BINANCE_API_SECRET || '').trim();
const TELEGRAM_TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const TELEGRAM_CHAT_ID = (process.env.TELEGRAM_CHAT_ID || '').trim();
const LIVE_ENABLED = String(process.env.EXECUTOR_LIVE_ENABLED || 'true').toLowerCase() === 'true';
const MAX_QUOTE_USDT = Number(process.env.EXECUTOR_MAX_QUOTE_USDT || '10');
const DAILY_LOSS_LIMIT_USDT = Number(process.env.DAILY_LOSS_LIMIT_USDT || '2');

function b64urlDecode(s) {
  const padded = s + '='.repeat((4 - (s.length % 4)) % 4);
  return Buffer.from(padded, 'base64url');
}
function signingKey() {
  if (!API_SECRET) return null;
  return crypto.createHash('sha256').update(`tst-executor-v1:${API_SECRET}`).digest();
}
function parseToken(token) {
  try {
    const [payload, mac] = token.split('.');
    if (!payload || !mac) return null;
    const key = signingKey();
    if (!key) return null;
    const expected = crypto.createHmac('sha256', key).update(payload).digest('hex').slice(0, 32);
    if (!crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(expected))) return null;
    const data = JSON.parse(b64urlDecode(payload).toString('utf8'));
    if (!data || Date.now() / 1000 > Number(data.exp || 0)) return null;
    if (!/^[A-Z0-9]{2,20}\/USDT$/.test(data.pair || '')) return null;
    if (!(Number(data.stake_usdt) > 0 && Number(data.entry) > 0 && Number(data.tp) > Number(data.entry) && Number(data.sl) < Number(data.entry))) return null;
    return data;
  } catch {
    return null;
  }
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function page(title, body) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title><style>*{box-sizing:border-box}body{margin:0;background:#0b0e11;color:#eaecef;font-family:Arial,sans-serif;padding:18px}.card{max-width:520px;margin:18px auto;background:#181a20;border:1px solid #2b3139;border-radius:18px;padding:22px}.badge{display:inline-block;padding:6px 10px;border-radius:8px;background:#2b3139;color:#fcd535;font-size:12px;font-weight:800}h1{font-size:25px;margin:12px 0}.row{display:flex;justify-content:space-between;gap:18px;padding:11px 0;border-bottom:1px solid #2b3139}.label{color:#848e9c}.val{font-weight:800;text-align:right}.green{color:#0ecb81}.red{color:#f6465d}.warn{margin:16px 0;padding:12px;background:#252a31;border-radius:10px;line-height:1.5;font-size:13px}button{width:100%;border:0;border-radius:11px;padding:16px;background:#fcd535;color:#181a20;font-weight:900;font-size:16px}.ok{color:#0ecb81}.bad{color:#f6465d}.small{color:#848e9c;font-size:12px;line-height:1.5;margin-top:14px}</style></head><body><div class="card">${body}</div></body></html>`;
}
function json(res, status, data) {
  res.status(status).setHeader('Content-Type','application/json; charset=utf-8').setHeader('Cache-Control','no-store').send(JSON.stringify(data));
}
function html(res, status, body) {
  res.status(status).setHeader('Content-Type','text/html; charset=utf-8').setHeader('Cache-Control','no-store').send(body);
}
function normalizeParams(params) {
  return Object.entries(params).filter(([,v]) => v !== undefined && v !== null && v !== '').map(([k,v]) => [k, String(v)]);
}
async function signed(path, method='GET', params={}) {
  if (!API_KEY || !API_SECRET) throw new Error('BINANCE_API_KEY/BINANCE_API_SECRET missing on executor');
  let lastError = 'unknown';
  for (const base of API_BASES) {
    try {
      const pairs = normalizeParams({...params, recvWindow: 5000, timestamp: Date.now()});
      const qs = new URLSearchParams(pairs).toString();
      const sig = crypto.createHmac('sha256', API_SECRET).update(qs).digest('hex');
      const full = `${qs}&signature=${sig}`;
      const url = `${base}${path}${method === 'GET' ? `?${full}` : ''}`;
      const response = await fetch(url, {
        method,
        headers: {'X-MBX-APIKEY': API_KEY, 'Content-Type':'application/x-www-form-urlencoded','User-Agent':'tst-one-tap-executor/1.0'},
        body: method === 'GET' ? undefined : full,
        signal: AbortSignal.timeout(15000),
      });
      const text = await response.text();
      let data; try { data = JSON.parse(text); } catch { data = {raw:text}; }
      if (!response.ok || (typeof data?.code === 'number' && data.code < 0)) {
        lastError = `${base} HTTP ${response.status}: ${data?.code ?? ''} ${data?.msg ?? text.slice(0,120)}`;
        if ([401,403,451].includes(response.status)) continue;
        const err = new Error(lastError); err.binance = data; throw err;
      }
      return data;
    } catch (e) {
      lastError = e.message;
      if (e.binance) throw e;
    }
  }
  throw new Error(`All Binance private endpoints failed: ${lastError}`);
}
async function publicGet(path) {
  let last = 'unknown';
  for (const base of API_BASES) {
    try {
      const r = await fetch(base + path, {headers:{'User-Agent':'tst-one-tap-executor/1.0'}, signal:AbortSignal.timeout(12000)});
      if (!r.ok) { last = `HTTP ${r.status}`; continue; }
      return await r.json();
    } catch (e) { last = e.message; }
  }
  throw new Error(`Binance public unavailable: ${last}`);
}
function decimals(step) {
  const s = String(step);
  if (s.includes('e-')) return Number(s.split('e-')[1]);
  return (s.split('.')[1] || '').replace(/0+$/,'').length;
}
function floorStep(v, step) {
  const d = decimals(step);
  return (Math.floor((v + 1e-12) / step) * step).toFixed(d);
}
function roundTick(v, tick, mode='nearest') {
  const d = decimals(tick);
  const n = v / tick;
  const k = mode === 'down' ? Math.floor(n + 1e-12) : mode === 'up' ? Math.ceil(n - 1e-12) : Math.round(n);
  return (k * tick).toFixed(d);
}
async function marketInfo(symbol) {
  const info = await publicGet(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`);
  const s = info.symbols?.[0];
  if (!s || s.status !== 'TRADING' || !s.isSpotTradingAllowed) throw new Error(`${symbol} is not Spot TRADING`);
  const lot = s.filters.find(f => f.filterType === 'LOT_SIZE');
  const price = s.filters.find(f => f.filterType === 'PRICE_FILTER');
  const notional = s.filters.find(f => f.filterType === 'NOTIONAL' || f.filterType === 'MIN_NOTIONAL');
  return {step:Number(lot?.stepSize || 1e-8), minQty:Number(lot?.minQty || 0), tick:Number(price?.tickSize || 1e-8), minNotional:Number(notional?.minNotional || 5)};
}
async function getOrderByClient(symbol, clientId) {
  try { return await signed('/api/v3/order','GET',{symbol, origClientOrderId:clientId}); }
  catch (e) {
    if (e.binance?.code === -2013) return null;
    throw e;
  }
}
async function placeOrRecoverBuy(symbol, quoteQty, clientId) {
  const existing = await getOrderByClient(symbol, clientId);
  if (existing) return {...existing, recovered:true};
  return signed('/api/v3/order','POST',{
    symbol, side:'BUY', type:'MARKET', quoteOrderQty:quoteQty.toFixed(2), newClientOrderId:clientId, newOrderRespType:'FULL'
  });
}
async function queryOco(listClientOrderId) {
  try { return await signed('/api/v3/orderList','GET',{origClientOrderId:listClientOrderId}); }
  catch (e) {
    if (e.binance?.code === -2022 || e.binance?.code === -2013) return null;
    return null;
  }
}
async function placeOco(symbol, quantity, tp, slTrigger, slLimit, listClientOrderId) {
  const existing = await queryOco(listClientOrderId);
  if (existing) return {...existing, recovered:true};
  return signed('/api/v3/orderList/oco','POST',{
    symbol, side:'SELL', quantity,
    listClientOrderId,
    aboveType:'LIMIT_MAKER', abovePrice:tp,
    belowType:'STOP_LOSS_LIMIT', belowStopPrice:slTrigger, belowPrice:slLimit, belowTimeInForce:'GTC',
    newOrderRespType:'RESULT'
  });
}
async function telegram(text) {
  if (!TELEGRAM_TOKEN || !TELEGRAM_CHAT_ID) return false;
  try {
    const r = await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:TELEGRAM_CHAT_ID,text,disable_web_page_preview:true}),signal:AbortSignal.timeout(10000)});
    return r.ok;
  } catch { return false; }
}
async function execute(sig) {
  if (!LIVE_ENABLED) throw new Error('Executor is not live-enabled');
  const symbol = sig.pair.replace('/','');
  const quoteQty = Math.min(Number(sig.stake_usdt), MAX_QUOTE_USDT);
  if (!(quoteQty >= 5 && quoteQty <= MAX_QUOTE_USDT)) throw new Error(`Quote amount ${quoteQty} outside executor bounds`);
  const info = await marketInfo(symbol);
  if (quoteQty < info.minNotional) throw new Error(`Amount below Binance minNotional ${info.minNotional}`);

  const buyClient = `tstb_${sig.id}`.slice(0,32);
  const buy = await placeOrRecoverBuy(symbol, quoteQty, buyClient);
  const executedQty = Number(buy.executedQty || 0);
  const quoteSpent = Number(buy.cummulativeQuoteQty || buy.cumulativeQuoteQty || 0);
  if (!(executedQty > 0 && quoteSpent > 0)) throw new Error(`BUY not filled: status=${buy.status || 'unknown'}`);
  const avg = quoteSpent / executedQty;

  // Preserve the signal's percentage distances, but anchor them to the actual fill.
  const tpPct = Math.max(0.002, Number(sig.tp) / Number(sig.entry) - 1);
  const slPct = Math.max(0.002, 1 - Number(sig.sl) / Number(sig.entry));
  const tpRaw = avg * (1 + tpPct);
  const slTriggerRaw = avg * (1 - slPct);
  const slLimitRaw = slTriggerRaw * 0.9985;
  const qty = floorStep(executedQty * 0.999, info.step); // leave tiny commission/dust buffer
  if (Number(qty) < info.minQty) throw new Error(`Filled quantity ${qty} below minQty ${info.minQty}`);
  const tp = roundTick(tpRaw, info.tick, 'up');
  const slTrigger = roundTick(slTriggerRaw, info.tick, 'down');
  const slLimit = roundTick(slLimitRaw, info.tick, 'down');
  const ocoClient = `tsto_${sig.id}`.slice(0,32);
  let protection = null;
  let protectionError = null;
  try {
    protection = await placeOco(symbol, qty, tp, slTrigger, slLimit, ocoClient);
  } catch (e) {
    protectionError = e.message;
  }

  if (protectionError) {
    await telegram(`🚨 PROTECTION FAILED — ${sig.pair}\nBUY executed: ${quoteSpent.toFixed(4)} USDT @ avg ${avg.toPrecision(8)}\nQty: ${executedQty}\nTP/SL was NOT placed.\nError: ${protectionError}\nافتحي Binance فورًا وضعي الحماية يدويًا.`);
  } else {
    await telegram(`✅ BUY EXECUTED + PROTECTED — ${sig.pair}\nSpent: ${quoteSpent.toFixed(4)} USDT\nAvg fill: ${avg.toPrecision(8)}\nProtected qty: ${qty}\nTP: ${tp}\nSL trigger: ${slTrigger}\nSL limit: ${slLimit}`);
  }
  return {symbol, quoteSpent, executedQty, avg, qty, tp, slTrigger, slLimit, protectionOk:!protectionError, protectionError, buyOrderId:buy.orderId, orderListId:protection?.orderListId};
}

export default async function handler(req, res) {
  try {
    const action = req.query?.action;
    if (action === 'preflight') {
      const configured = Boolean(API_KEY && API_SECRET);
      if (!configured) return json(res, 503, {ok:false, configured:false, liveEnabled:LIVE_ENABLED, error:'Binance executor env vars missing'});
      const account = await signed('/api/v3/account','GET',{omitZeroBalances:'true'});
      return json(res, 200, {ok:true, configured:true, liveEnabled:LIVE_ENABLED, canTrade:Boolean(account.canTrade), accountType:account.accountType || null, permissions:account.permissions || [], dailyLossLimit:DAILY_LOSS_LIMIT_USDT, maxQuoteUsdt:MAX_QUOTE_USDT});
    }

    const token = req.method === 'POST' ? (req.body?.t || req.query?.t || '') : (req.query?.t || '');
    const sig = parseToken(token);
    if (!sig) return html(res, 400, page('Invalid signal','<div class="badge">INVALID / EXPIRED</div><h1 class="bad">Signal invalid or expired</h1><div class="small">اطلبي إشارة جديدة من البوت. لن يتم تنفيذ أي شراء.</div>'));

    if (req.method === 'GET') {
      const stake = Math.min(Number(sig.stake_usdt), MAX_QUOTE_USDT);
      const tpPct = (Number(sig.tp)/Number(sig.entry)-1)*100;
      const slPct = (1-Number(sig.sl)/Number(sig.entry))*100;
      const body = `<div class="badge">ONE-TAP SPOT · MANUAL CONFIRM</div><h1>${esc(sig.pair)}</h1><div class="row"><span class="label">Spend</span><span class="val">${stake.toFixed(2)} USDT</span></div><div class="row"><span class="label">Order</span><span class="val">MARKET BUY</span></div><div class="row"><span class="label">TP distance</span><span class="val green">+${tpPct.toFixed(2)}%</span></div><div class="row"><span class="label">SL distance</span><span class="val red">-${slPct.toFixed(2)}%</span></div><div class="warn">بعد التنفيذ، TP وStop Loss بيتحسبوا على <b>سعر التنفيذ الحقيقي</b> ويتحطوا OCO تلقائيًا. الضغط على الزر أدناه هو الموافقة النهائية على شراء حقيقي من Binance Spot.</div><form method="post"><input type="hidden" name="t" value="${esc(token)}"><button type="submit">⚡ CONFIRM BUY ${stake.toFixed(2)} USDT</button></form><div class="small">No Futures · No leverage · No withdrawal. لو الحماية فشلت بعد الشراء هيوصلك تنبيه أحمر فورًا.</div>`;
      return html(res, 200, page(`${sig.pair} Confirm Buy`, body));
    }
    if (req.method !== 'POST') return json(res,405,{ok:false,error:'Method not allowed'});

    const result = await execute(sig);
    const status = result.protectionOk ? '<h1 class="ok">✅ BUY EXECUTED + PROTECTED</h1>' : '<h1 class="bad">🚨 BUY EXECUTED — PROTECTION FAILED</h1>';
    const body = `<div class="badge">BINANCE SPOT RESULT</div>${status}<div class="row"><span class="label">Pair</span><span class="val">${esc(sig.pair)}</span></div><div class="row"><span class="label">Spent</span><span class="val">${result.quoteSpent.toFixed(4)} USDT</span></div><div class="row"><span class="label">Average fill</span><span class="val">${result.avg.toPrecision(8)}</span></div><div class="row"><span class="label">TP</span><span class="val green">${esc(result.tp)}</span></div><div class="row"><span class="label">SL Trigger</span><span class="val red">${esc(result.slTrigger)}</span></div><div class="row"><span class="label">SL Limit</span><span class="val red">${esc(result.slLimit)}</span></div>${result.protectionOk?'':'<div class="warn bad">TP/SL لم يتم وضعهما. افتحي Binance فورًا وضعي الحماية يدويًا.</div>'}`;
    return html(res, 200, page('Execution result', body));
  } catch (e) {
    await telegram(`🚨 EXECUTOR ERROR\n${String(e.message).slice(0,500)}`);
    return html(res, 503, page('Executor error', `<div class="badge">EXECUTOR ERROR</div><h1 class="bad">لم يتم إكمال العملية</h1><div class="warn">${esc(e.message)}</div><div class="small">لو الشراء تم قبل ظهور الخطأ، راجعي Binance Open Orders/Spot balance فورًا. البوت يستخدم Client IDs ثابتة لمنع تكرار الشراء عند إعادة المحاولة.</div>`));
  }
}
