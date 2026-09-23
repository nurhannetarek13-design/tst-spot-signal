import crypto from "node:crypto";

const BINANCE_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

const ALLOWED = new Set([
  "GET /api/v3/account",
  "POST /api/v3/order",
  "POST /api/v3/orderList/oco",
]);

function safeEqualHex(a, b) {
  const aa=String(a||"").toLowerCase(), bb=String(b||"").toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(aa) || !/^[a-f0-9]{64}$/.test(bb)) return false;
  return crypto.timingSafeEqual(Buffer.from(aa,"hex"),Buffer.from(bb,"hex"));
}

function verifyEnvelope(req, raw) {
  const secret=String(process.env.TELEGRAM_BOT_TOKEN||"");
  if (!secret) return {ok:false,status:"RELAY_SECRET_MISSING"};
  const ts=String(req.headers["x-executor-timestamp"]||"");
  const sig=String(req.headers["x-executor-signature"]||"");
  const stamp=Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now()-stamp)>60_000) {
    return {ok:false,status:"STALE_RELAY_REQUEST"};
  }
  const expected=crypto.createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");
  return safeEqualHex(expected,sig) ? {ok:true} : {ok:false,status:"BAD_RELAY_SIGNATURE"};
}

function cleanParams(method,path,params) {
  const p={};
  for (const [k,v] of Object.entries(params||{})) {
    if (v===undefined || v===null || v==="") continue;
    p[k]=String(v);
  }
  if (method==="GET" && path==="/api/v3/account") {
    return {};
  }
  if (method==="POST" && path==="/api/v3/order") {
    const allowed=["symbol","side","type","quoteOrderQty","quantity","newOrderRespType","newClientOrderId"];
    for (const k of Object.keys(p)) if (!allowed.includes(k)) delete p[k];
    if (!/^[A-Z0-9]{2,20}USDT$/.test(p.symbol||"")) throw new Error("BAD_SYMBOL");
    if (!["BUY","SELL"].includes(p.side||"")) throw new Error("BAD_SIDE");
    if (p.type!=="MARKET") throw new Error("ONLY_MARKET_ORDER_ALLOWED");
    if (p.side==="BUY" && !(Number(p.quoteOrderQty)>=5 && Number(p.quoteOrderQty)<=10)) throw new Error("BAD_BUY_NOTIONAL");
    if (p.side==="SELL" && !(Number(p.quantity)>0)) throw new Error("BAD_SELL_QUANTITY");
    return p;
  }
  if (method==="POST" && path==="/api/v3/orderList/oco") {
    const allowed=["symbol","side","quantity","aboveType","abovePrice","belowType","belowStopPrice","belowPrice","belowTimeInForce","listClientOrderId"];
    for (const k of Object.keys(p)) if (!allowed.includes(k)) delete p[k];
    if (!/^[A-Z0-9]{2,20}USDT$/.test(p.symbol||"")) throw new Error("BAD_SYMBOL");
    if (p.side!=="SELL") throw new Error("OCO_SELL_ONLY");
    if (!(Number(p.quantity)>0 && Number(p.abovePrice)>0 && Number(p.belowStopPrice)>0 && Number(p.belowPrice)>0)) {
      throw new Error("BAD_OCO_LEVELS");
    }
    return p;
  }
  throw new Error("OPERATION_NOT_ALLOWED");
}

function canonicalQuery(params) {
  return Object.entries(params)
    .sort(([a],[b]) => a.localeCompare(b))
    .map(([k,v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
}

async function binanceForward(method,path,params) {
  const apiKey=String(process.env.BINANCE_API_KEY||"").trim();
  const secret=String(process.env.BINANCE_API_SECRET||"").trim();
  if (!apiKey || !secret) return {ok:false,upstream:{status:503,code:null,msg:"BINANCE_CREDENTIALS_MISSING"}};

  const unsigned={...params,recvWindow:"5000",timestamp:String(Date.now())};
  const payload=canonicalQuery(unsigned);
  const signature=crypto.createHmac("sha256",secret).update(payload).digest("hex");
  const query=`${payload}&signature=${signature}`;

  let last={status:502,code:null,msg:"BINANCE_UNAVAILABLE"};
  for (const base of BINANCE_BASES) {
    try {
      const r=await fetch(`${base}${path}?${query}`,{
        method,
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const text=await r.text();
      let data={}; try { data=JSON.parse(text||"{}"); } catch { data={}; }
      if (r.ok && !(Number(data?.code)<0)) return {ok:true,data};
      last={status:r.status,code:data?.code??null,msg:String(data?.msg||"UPSTREAM_REJECTED").slice(0,120)};
      if (Number(last.code)<0) break;
    } catch(e) {
      last={status:502,code:null,msg:String(e?.message||e).slice(0,120)};
    }
  }
  return {ok:false,upstream:last};
}

export default async function handler(req,res) {
  res.setHeader("Cache-Control","no-store");
  if (req.method!=="POST") return res.status(405).json({ok:false,status:"METHOD_NOT_ALLOWED"});

  const raw=typeof req.body==="string" ? req.body : JSON.stringify(req.body||{});
  const auth=verifyEnvelope(req,raw);
  if (!auth.ok) return res.status(401).json({...auth,tradingAction:"NONE"});

  let body={};
  try { body=typeof req.body==="object" ? req.body : JSON.parse(raw||"{}"); }
  catch { return res.status(400).json({ok:false,status:"BAD_JSON",tradingAction:"NONE"}); }

  const method=String(body.method||"").toUpperCase();
  const path=String(body.path||"");
  const network=String(body.network||"");
  const op=`${method} ${path}`;

  if (network!=="production") return res.status(400).json({ok:false,status:"PRODUCTION_ONLY"});
  if (!ALLOWED.has(op)) return res.status(403).json({ok:false,status:"OPERATION_NOT_ALLOWED",tradingAction:"NONE"});

  let params;
  try { params=cleanParams(method,path,body.params||{}); }
  catch(e) { return res.status(400).json({ok:false,status:String(e?.message||"BAD_PARAMS"),tradingAction:"NONE"}); }

  const out=await binanceForward(method,path,params);
  if (!out.ok) {
    const status=out.upstream?.status>=400 && out.upstream?.status<600 ? out.upstream.status : 502;
    return res.status(status).json({ok:false,status:"BINANCE_UPSTREAM_REJECTED",upstream:out.upstream});
  }

  return res.status(200).json({
    ok:true,
    status:"SIGNED_BINANCE_RELAY_OK",
    data:out.data,
    noSecretValuesExposed:true,
  });
}
