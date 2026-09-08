const API_BASES = [
  'https://api.binance.com', 'https://api-gcp.binance.com', 'https://api1.binance.com',
  'https://api2.binance.com', 'https://api3.binance.com', 'https://api4.binance.com',
];
const SIGNER = (process.env.ORDER_SIGNER_URL || 'https://freqtrade-production-43ed.up.railway.app/signer').replace(/\/$/, '');
const TELEGRAM_TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const TELEGRAM_CHAT_ID = (process.env.TELEGRAM_CHAT_ID || '').trim();
const MAX_QUOTE_USDT = Number(process.env.EXECUTOR_MAX_QUOTE_USDT || '10');

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function page(title, body) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title><style>*{box-sizing:border-box}body{margin:0;background:#0b0e11;color:#eaecef;font-family:Arial,sans-serif;padding:18px}.card{max-width:520px;margin:18px auto;background:#181a20;border:1px solid #2b3139;border-radius:18px;padding:22px}.badge{display:inline-block;padding:6px 10px;border-radius:8px;background:#2b3139;color:#fcd535;font-size:12px;font-weight:800}h1{font-size:25px;margin:12px 0}.row{display:flex;justify-content:space-between;gap:18px;padding:11px 0;border-bottom:1px solid #2b3139}.label{color:#848e9c}.val{font-weight:800;text-align:right}.green{color:#0ecb81}.red{color:#f6465d}.warn{margin:16px 0;padding:12px;background:#252a31;border-radius:10px;line-height:1.5;font-size:13px}button{width:100%;border:0;border-radius:11px;padding:16px;background:#fcd535;color:#181a20;font-weight:900;font-size:16px}.ok{color:#0ecb81}.bad{color:#f6465d}.small{color:#848e9c;font-size:12px;line-height:1.5;margin-top:14px}</style></head><body><div class="card">${body}</div></body></html>`;
}
function html(res, status, body) { res.status(status).setHeader('Content-Type','text/html; charset=utf-8').setHeader('Cache-Control','no-store').send(body); }
function json(res, status, data) { res.status(status).setHeader('Cache-Control','no-store').json(data); }

async function signerValidate(t) {
  const r = await fetch(`${SIGNER}/validate?t=${encodeURIComponent(t)}`, {headers:{Accept:'application/json'}, signal:AbortSignal.timeout(10000)});
  const data = await r.json().catch(()=>({}));
  if (!r.ok || !data.ok) throw new Error(`Signer validation failed: ${data.error || r.status}`);
  return data.signal;
}
async function signerSpec(t, op, extra={}) {
  const r = await fetch(`${SIGNER}/sign`, {method:'POST', headers:{'Content-Type':'application/json',Accept:'application/json'}, body:JSON.stringify({t,op,...extra}), signal:AbortSignal.timeout(10000)});
  const data = await r.json().catch(()=>({}));
  if (!r.ok || !data.ok || !data.spec) throw new Error(`Signer ${op} failed: ${data.error || r.status}`);
  return data.spec;
}
async function forwardSpec(spec) {
  let last = 'unknown';
  for (const base of API_BASES) {
    try {
      const full = `${spec.query}&signature=${spec.signature}`;
      const r = await fetch(`${base}${spec.path}${spec.method === 'GET' ? `?${full}` : ''}`, {
        method:spec.method,
        headers:{'X-MBX-APIKEY':spec.apiKey,'Content-Type':'application/x-www-form-urlencoded','User-Agent':'tst-one-tap-executor/2.0'},
        body:spec.method === 'GET' ? undefined : full,
        signal:AbortSignal.timeout(15000),
      });
      const text=await r.text(); let data; try{data=JSON.parse(text)}catch{data={raw:text}}
      if (r.status===451) { last=`${base}:451`; continue; }
      return {ok:r.ok && !(typeof data?.code==='number' && data.code<0), status:r.status, data, base};
    } catch(e) { last=`${base}:${e.message}`; }
  }
  throw new Error(`All Binance endpoints unavailable (${last})`);
}
async function privateOp(t, op, extra={}) { return forwardSpec(await signerSpec(t,op,extra)); }
async function publicGet(path) {
  let last='unknown';
  for(const base of API_BASES){try{const r=await fetch(base+path,{headers:{'User-Agent':'tst-one-tap-executor/2.0'},signal:AbortSignal.timeout(12000)});if(!r.ok){last=`HTTP ${r.status}`;continue}return await r.json()}catch(e){last=e.message}}
  throw new Error(`Binance public unavailable: ${last}`);
}
function decimals(step){const s=String(step);if(s.includes('e-'))return Number(s.split('e-')[1]);return(s.split('.')[1]||'').replace(/0+$/,'').length}
function floorStep(v,step){const d=decimals(step);return(Math.floor((v+1e-12)/step)*step).toFixed(d)}
function roundTick(v,tick,mode='nearest'){const d=decimals(tick),n=v/tick,k=mode==='down'?Math.floor(n+1e-12):mode==='up'?Math.ceil(n-1e-12):Math.round(n);return(k*tick).toFixed(d)}
async function marketInfo(symbol){const info=await publicGet(`/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`),s=info.symbols?.[0];if(!s||s.status!=='TRADING'||!s.isSpotTradingAllowed)throw new Error(`${symbol} is not Spot TRADING`);const lot=s.filters.find(f=>f.filterType==='LOT_SIZE'),price=s.filters.find(f=>f.filterType==='PRICE_FILTER'),notional=s.filters.find(f=>f.filterType==='NOTIONAL'||f.filterType==='MIN_NOTIONAL');return{step:Number(lot?.stepSize||1e-8),minQty:Number(lot?.minQty||0),tick:Number(price?.tickSize||1e-8),minNotional:Number(notional?.minNotional||5)}}
async function telegram(text){if(!TELEGRAM_TOKEN||!TELEGRAM_CHAT_ID)return false;try{const r=await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:TELEGRAM_CHAT_ID,text,disable_web_page_preview:true}),signal:AbortSignal.timeout(10000)});return r.ok}catch{return false}}
function binanceError(result,label){const d=result?.data||{};return new Error(`${label}: ${d.code??result?.status??''} ${d.msg||d.raw||'request failed'}`)}

async function queryBuy(t){const r=await privateOp(t,'query_buy');if(r.ok)return r.data;if(r.data?.code===-2013)return null;throw binanceError(r,'Query BUY')}
async function queryOco(t){const r=await privateOp(t,'query_oco');if(r.ok)return r.data;if([-2013,-2022].includes(r.data?.code))return null;return null}

async function execute(t,sig){
  const symbol=sig.pair.replace('/',''), baseAsset=sig.pair.split('/')[0], quoteQty=Math.min(Number(sig.stake_usdt),MAX_QUOTE_USDT);
  if(!(quoteQty>=5&&quoteQty<=MAX_QUOTE_USDT))throw new Error(`Amount ${quoteQty} outside executor bounds`);
  const info=await marketInfo(symbol);if(quoteQty<info.minNotional)throw new Error(`Amount below Binance minimum ${info.minNotional} USDT`);

  let buy=await queryBuy(t);
  if(!buy){
    const r=await privateOp(t,'buy');
    if(r.ok) buy=r.data;
    else {
      buy=await queryBuy(t).catch(()=>null);
      if(!buy) throw binanceError(r,'BUY');
    }
  }
  const executedQty=Number(buy.executedQty||0),quoteSpent=Number(buy.cummulativeQuoteQty||buy.cumulativeQuoteQty||0);
  if(!(executedQty>0&&quoteSpent>0))throw new Error(`BUY not filled: ${buy.status||'unknown'}`);
  const avg=quoteSpent/executedQty;
  const baseCommission=(buy.fills||[]).filter(f=>f.commissionAsset===baseAsset).reduce((s,f)=>s+Number(f.commission||0),0);
  const availableQty=Math.max(0,executedQty-baseCommission);
  const tpPct=Math.max(0.002,Number(sig.tp)/Number(sig.entry)-1),slPct=Math.max(0.002,1-Number(sig.sl)/Number(sig.entry));
  const qty=floorStep(availableQty,info.step);if(Number(qty)<info.minQty)throw new Error(`Protected quantity ${qty} below Binance minQty ${info.minQty}`);
  const tp=roundTick(avg*(1+tpPct),info.tick,'up'),slTrigger=roundTick(avg*(1-slPct),info.tick,'down'),slLimit=roundTick(avg*(1-slPct)*0.9985,info.tick,'down');

  let protection=await queryOco(t),protectionError=null;
  if(!protection){
    const r=await privateOp(t,'oco',{quantity:qty,tp,slTrigger,slLimit});
    if(r.ok) protection=r.data; else protectionError=`${r.data?.code??r.status} ${r.data?.msg||'OCO failed'}`;
  }
  if(protectionError){
    await telegram(`🚨 PROTECTION FAILED — ${sig.pair}\nBUY executed: ${quoteSpent.toFixed(4)} USDT @ ${avg.toPrecision(8)}\nTP/SL NOT placed.\n${protectionError}\nافتحي Binance فورًا وضعي الحماية يدويًا.`);
  }else{
    await telegram(`✅ BUY EXECUTED + PROTECTED — ${sig.pair}\nSpent: ${quoteSpent.toFixed(4)} USDT\nAvg fill: ${avg.toPrecision(8)}\nTP: ${tp}\nSL trigger: ${slTrigger}\nSL limit: ${slLimit}`);
  }
  return{quoteSpent,executedQty,avg,qty,tp,slTrigger,slLimit,protectionOk:!protectionError,protectionError,buyOrderId:buy.orderId,orderListId:protection?.orderListId};
}

export default async function handler(req,res){
  try{
    if(req.query?.action==='preflight'){
      const h=await fetch(`${SIGNER}/health`,{signal:AbortSignal.timeout(10000)});const d=await h.json().catch(()=>({}));
      return json(res,h.ok?200:503,{ok:h.ok&&d.ok,signer:SIGNER,signerConfigured:Boolean(d.configured),binancePublic:Boolean(await publicGet('/api/v3/time').catch(()=>null))});
    }
    const t=String(req.query?.t||req.body?.t||'');
    const sig=await signerValidate(t);
    if(req.method==='GET'){
      const accountResult=await privateOp(t,'account');
      const canTrade=Boolean(accountResult.ok&&accountResult.data?.canTrade);
      const stake=Math.min(Number(sig.stake_usdt),MAX_QUOTE_USDT),tpPct=(Number(sig.tp)/Number(sig.entry)-1)*100,slPct=(1-Number(sig.sl)/Number(sig.entry))*100;
      const status=canTrade?'<div class="badge">ACCOUNT PREFLIGHT ✓</div>':'<div class="badge bad">ACCOUNT PREFLIGHT FAILED</div>';
      const button=canTrade?`<form method="post" action="/api/executor?t=${encodeURIComponent(t)}"><button type="submit">⚡ CONFIRM BUY ${stake.toFixed(2)} USDT</button></form>`:`<div class="warn bad">Binance account is not ready for trading. No order will be sent.</div>`;
      const body=`${status}<h1>${esc(sig.pair)}</h1><div class="row"><span class="label">Spend</span><span class="val">${stake.toFixed(2)} USDT</span></div><div class="row"><span class="label">Order</span><span class="val">MARKET BUY</span></div><div class="row"><span class="label">TP distance</span><span class="val green">+${tpPct.toFixed(2)}%</span></div><div class="row"><span class="label">SL distance</span><span class="val red">-${slPct.toFixed(2)}%</span></div><div class="warn">فتح الصفحة لا يشتري. الشراء الحقيقي يحصل فقط عند الضغط على CONFIRM BUY. بعد الـfill، TP/SL بيتحسبوا على سعر التنفيذ الحقيقي ويتحطوا OCO تلقائيًا.</div>${button}<div class="small">Spot only · No leverage · No withdrawal.</div>`;
      return html(res,200,page(`${sig.pair} Confirm`,body));
    }
    if(req.method!=='POST')return json(res,405,{ok:false,error:'Method not allowed'});
    const result=await execute(t,sig);
    const head=result.protectionOk?'<h1 class="ok">✅ BUY EXECUTED + PROTECTED</h1>':'<h1 class="bad">🚨 BUY EXECUTED — PROTECTION FAILED</h1>';
    const body=`<div class="badge">BINANCE SPOT RESULT</div>${head}<div class="row"><span class="label">Pair</span><span class="val">${esc(sig.pair)}</span></div><div class="row"><span class="label">Spent</span><span class="val">${result.quoteSpent.toFixed(4)} USDT</span></div><div class="row"><span class="label">Average fill</span><span class="val">${result.avg.toPrecision(8)}</span></div><div class="row"><span class="label">TP</span><span class="val green">${esc(result.tp)}</span></div><div class="row"><span class="label">SL Trigger</span><span class="val red">${esc(result.slTrigger)}</span></div><div class="row"><span class="label">SL Limit</span><span class="val red">${esc(result.slLimit)}</span></div>${result.protectionOk?'':'<div class="warn bad">TP/SL failed. Open Binance immediately and protect the position manually.</div>'}`;
    return html(res,200,page('Execution result',body));
  }catch(e){await telegram(`🚨 EXECUTOR ERROR\n${String(e.message).slice(0,500)}`);return html(res,503,page('Executor error',`<div class="badge">EXECUTOR ERROR</div><h1 class="bad">لم يتم إكمال العملية</h1><div class="warn">${esc(e.message)}</div><div class="small">لو ظهر الخطأ بعد ضغط CONFIRM، راجعي Binance Spot/Open Orders. Client IDs ثابتة لمنع تكرار الشراء.</div>`))}
}
