import crypto from "node:crypto";

const BINANCE_GET_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];
const BINANCE_WRITE_BASE = "https://api.binance.com";
const MAX_BUY_QUOTE_USDT = 10;
const MIN_BUY_QUOTE_USDT = 5;

function json(res, status, body) {
  res.setHeader("Cache-Control", "no-store");
  return res.status(status).json(body);
}

function safeEqualHex(a, b) {
  const aa=String(a||"").toLowerCase(), bb=String(b||"").toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(aa) || !/^[a-f0-9]{64}$/.test(bb)) return false;
  return crypto.timingSafeEqual(Buffer.from(aa,"hex"),Buffer.from(bb,"hex"));
}

function verifyEnvelope(req, raw) {
  const secret=String(process.env.TELEGRAM_BOT_TOKEN||"").trim();
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

function plainObject(value) {
  return Boolean(value) && typeof value==="object" && !Array.isArray(value) && Object.getPrototypeOf(value)===Object.prototype;
}

function exactKeys(params, allowed) {
  const keys=Object.keys(params).sort();
  const expected=[...allowed].sort();
  return keys.length===expected.length && keys.every((k,i)=>k===expected[i]);
}

function safeSymbol(value) {
  return /^[A-Z0-9]{1,20}USDT$/.test(String(value||""));
}

function safeClientId(value, prefix) {
  const s=String(value||"");
  return s.startsWith(prefix) && /^[A-Za-z0-9_-]{5,36}$/.test(s);
}

function finitePositive(value) {
  const n=Number(value);
  return Number.isFinite(n) && n>0 ? n : null;
}

export function validateOperation(method, path, params) {
  if (!plainObject(params)) return {ok:false,status:"BAD_PARAMS"};

  if (method==="GET" && path==="/api/v3/account") {
    if (!exactKeys(params, [])) return {ok:false,status:"ACCOUNT_PARAMS_NOT_ALLOWED"};
    return {ok:true,params:{}};
  }

  if (method==="GET" && [
    "/sapi/v1/account/apiRestrictions",
    "/sapi/v1/account/status",
    "/sapi/v1/account/apiTradingStatus",
  ].includes(path)) {
    if (!exactKeys(params, [])) return {ok:false,status:"ACCOUNT_SAFETY_PARAMS_NOT_ALLOWED"};
    return {ok:true,params:{}};
  }

  if (method==="GET" && path==="/api/v3/order") {
    const allowed=["symbol","origClientOrderId"];
    if (!exactKeys(params,allowed) || !safeSymbol(params.symbol) ||
        !/^[A-Za-z0-9_-]{5,36}$/.test(String(params.origClientOrderId||""))) {
      return {ok:false,status:"ORDER_LOOKUP_PARAMS_NOT_ALLOWED"};
    }
    return {ok:true,params:{
      symbol:String(params.symbol),
      origClientOrderId:String(params.origClientOrderId),
    }};
  }

  if (method==="GET" && path==="/api/v3/orderList") {
    const allowed=["origClientOrderId"];
    if (!exactKeys(params,allowed) ||
        !/^[A-Za-z0-9_-]{5,36}$/.test(String(params.origClientOrderId||""))) {
      return {ok:false,status:"ORDER_LIST_LOOKUP_PARAMS_NOT_ALLOWED"};
    }
    return {ok:true,params:{origClientOrderId:String(params.origClientOrderId)}};
  }

  if (method==="POST" && path==="/api/v3/order") {
    const side=String(params.side||"");
    const type=String(params.type||"");
    if (type!=="MARKET" || !safeSymbol(params.symbol) || String(params.newOrderRespType||"")!=="FULL") {
      return {ok:false,status:"ORDER_SHAPE_NOT_ALLOWED"};
    }

    if (side==="BUY") {
      const allowed=["symbol","side","type","quoteOrderQty","newOrderRespType","newClientOrderId"];
      if (!exactKeys(params,allowed) || !safeClientId(params.newClientOrderId,"TSTU")) {
        return {ok:false,status:"BUY_PARAMS_NOT_ALLOWED"};
      }
      const quote=finitePositive(params.quoteOrderQty);
      if (quote===null || quote<MIN_BUY_QUOTE_USDT || quote>MAX_BUY_QUOTE_USDT) {
        return {ok:false,status:"BUY_QUOTE_OUTSIDE_LIMIT"};
      }
      return {ok:true,params:{
        symbol:String(params.symbol),
        side:"BUY",
        type:"MARKET",
        quoteOrderQty:Number(quote).toFixed(2),
        newOrderRespType:"FULL",
        newClientOrderId:String(params.newClientOrderId),
      }};
    }

    if (side==="SELL") {
      const allowed=["symbol","side","type","quantity","newOrderRespType","newClientOrderId"];
      if (!exactKeys(params,allowed) || !safeClientId(params.newClientOrderId,"TSTE")) {
        return {ok:false,status:"EMERGENCY_SELL_PARAMS_NOT_ALLOWED"};
      }
      const quantity=finitePositive(params.quantity);
      if (quantity===null || quantity>1e15) return {ok:false,status:"EMERGENCY_SELL_QTY_INVALID"};
      return {ok:true,params:{
        symbol:String(params.symbol),
        side:"SELL",
        type:"MARKET",
        quantity:String(params.quantity),
        newOrderRespType:"FULL",
        newClientOrderId:String(params.newClientOrderId),
      }};
    }

    return {ok:false,status:"ORDER_SIDE_NOT_ALLOWED"};
  }

  if (method==="POST" && path==="/api/v3/orderList/oco") {
    const allowed=[
      "symbol","side","quantity","aboveType","abovePrice","belowType",
      "belowStopPrice","belowPrice","belowTimeInForce","listClientOrderId",
    ];
    if (!exactKeys(params,allowed) || !safeSymbol(params.symbol) ||
        String(params.side)!=="SELL" ||
        String(params.aboveType)!=="LIMIT_MAKER" ||
        String(params.belowType)!=="STOP_LOSS_LIMIT" ||
        String(params.belowTimeInForce)!=="GTC" ||
        !safeClientId(params.listClientOrderId,"TSTO")) {
      return {ok:false,status:"OCO_PARAMS_NOT_ALLOWED"};
    }
    const quantity=finitePositive(params.quantity);
    const above=finitePositive(params.abovePrice);
    const stop=finitePositive(params.belowStopPrice);
    const below=finitePositive(params.belowPrice);
    if (quantity===null || above===null || stop===null || below===null || !(above>stop && stop>=below)) {
      return {ok:false,status:"OCO_GEOMETRY_INVALID"};
    }
    return {ok:true,params:{
      symbol:String(params.symbol),
      side:"SELL",
      quantity:String(params.quantity),
      aboveType:"LIMIT_MAKER",
      abovePrice:String(params.abovePrice),
      belowType:"STOP_LOSS_LIMIT",
      belowStopPrice:String(params.belowStopPrice),
      belowPrice:String(params.belowPrice),
      belowTimeInForce:"GTC",
      listClientOrderId:String(params.listClientOrderId),
    }};
  }

  return {ok:false,status:"OPERATION_NOT_ALLOWED"};
}

function normalizeCredential(value) {
  let s=String(value||"").trim();
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
    s=s.slice(1,-1).trim();
  }
  if (s.includes("\\n") && !s.includes("\n")) s=s.replaceAll("\\n","\n");
  return s;
}

function privateKeyFromSecret(secret) {
  const s=normalizeCredential(secret);
  const candidates=[];
  if (s.includes("BEGIN ") && s.includes("PRIVATE KEY")) candidates.push(s);
  if (/^[A-Za-z0-9+/=\s]{48,}$/.test(s) && !s.includes(" ")) {
    try {
      const der=Buffer.from(s.replace(/\s+/g,""),"base64");
      if (der.length>=32) candidates.push({key:der,format:"der",type:"pkcs8"});
    } catch {}
  }
  for (const candidate of candidates) {
    try { return crypto.createPrivateKey(candidate); } catch {}
  }
  return null;
}

function signPayload(secret, payload) {
  const normalized=normalizeCredential(secret);
  const privateKey=privateKeyFromSecret(normalized);
  if (!privateKey) {
    return {
      signature:crypto.createHmac("sha256",normalized).update(payload).digest("hex"),
      signerMode:"HMAC_SHA256",
    };
  }

  const type=String(privateKey.asymmetricKeyType||"").toLowerCase();
  if (type==="rsa" || type==="rsa-pss") {
    return {
      signature:crypto.sign("sha256",Buffer.from(payload,"utf8"),{
        key:privateKey,
        padding:crypto.constants.RSA_PKCS1_PADDING,
      }).toString("base64"),
      signerMode:"RSA_SHA256",
    };
  }
  if (type==="ed25519") {
    return {
      signature:crypto.sign(null,Buffer.from(payload,"utf8"),privateKey).toString("base64"),
      signerMode:"ED25519",
    };
  }
  throw new Error("UNSUPPORTED_BINANCE_PRIVATE_KEY_TYPE");
}

function signQuery(secret, params) {
  const queryParams=new URLSearchParams();
  for (const [key,value] of Object.entries(params)) queryParams.append(key,String(value));
  queryParams.append("recvWindow","5000");
  queryParams.append("timestamp",String(Date.now()));
  const unsigned=queryParams.toString();
  const signed=signPayload(secret,unsigned);
  return {
    query:`${unsigned}&signature=${encodeURIComponent(signed.signature)}`,
    signerMode:signed.signerMode,
  };
}

async function parseBinanceResponse(r) {
  const text=await r.text();
  let data={};
  try { data=JSON.parse(text||"{}"); } catch {}
  if (r.ok && !(Number(data?.code)<0)) return {ok:true,data};
  return {
    ok:false,
    upstream:{
      status:r.status,
      code:data?.code??null,
      msg:String(data?.msg||"upstream rejected").slice(0,160),
    },
  };
}

async function forwardGet(path, apiKey, query) {
  let last={status:502,code:null,msg:"Binance unavailable"};
  for (const base of BINANCE_GET_BASES) {
    try {
      const r=await fetch(`${base}${path}?${query}`,{
        method:"GET",
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const out=await parseBinanceResponse(r);
      if (out.ok) return out;
      last=out.upstream;
      if (Number(last.code)<0) break;
    } catch(e) {
      last={status:502,code:null,msg:String(e?.message||e).slice(0,160)};
    }
  }
  return {ok:false,upstream:last};
}

async function forwardWrite(path, apiKey, query) {
  try {
    const r=await fetch(`${BINANCE_WRITE_BASE}${path}?${query}`,{
      method:"POST",
      headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
      signal:AbortSignal.timeout(12_000),
    });
    return await parseBinanceResponse(r);
  } catch(e) {
    // Never retry an unknown POST result across alternate Binance hosts.
    return {ok:false,upstream:{status:502,code:null,msg:"WRITE_TRANSPORT_STATUS_UNKNOWN"}};
  }
}

export default async function handler(req,res) {
  if (req.method!=="POST") return json(res,405,{ok:false,status:"METHOD_NOT_ALLOWED"});

  const raw=typeof req.body==="string" ? req.body : JSON.stringify(req.body||{});
  const auth=verifyEnvelope(req,raw);
  if (!auth.ok) return json(res,401,{...auth,tradingAction:"NONE"});

  let body={};
  try { body=typeof req.body==="object" ? req.body : JSON.parse(raw||"{}"); }
  catch { return json(res,400,{ok:false,status:"BAD_JSON",tradingAction:"NONE"}); }

  const method=String(body.method||"").toUpperCase();
  const path=String(body.path||"");
  const network=String(body.network||"");
  if (network!=="production") return json(res,400,{ok:false,status:"PRODUCTION_ONLY",tradingAction:"NONE"});
  if (!exactKeys(body,["method","path","network","params"])) {
    return json(res,400,{ok:false,status:"RELAY_BODY_NOT_ALLOWED",tradingAction:"NONE"});
  }

  const checked=validateOperation(method,path,body.params);
  if (!checked.ok) return json(res,403,{ok:false,status:checked.status,tradingAction:"NONE"});

  const apiKey=normalizeCredential(process.env.BINANCE_API_KEY);
  const apiSecret=normalizeCredential(process.env.BINANCE_API_SECRET);
  if (!apiKey || !apiSecret) {
    return json(res,503,{ok:false,status:"VERCEL_BINANCE_CREDENTIALS_MISSING",tradingAction:"NONE"});
  }

  let signed;
  try { signed=signQuery(apiSecret,checked.params); }
  catch(e) {
    return json(res,503,{ok:false,status:String(e?.message||"BINANCE_SIGNER_FAILED"),tradingAction:"NONE"});
  }
  const out=method==="GET"
    ? await forwardGet(path,apiKey,signed.query)
    : await forwardWrite(path,apiKey,signed.query);
  if (!out.ok && out.upstream) out.upstream.signerMode=signed.signerMode;

  if (!out.ok) {
    const status=out.upstream?.status>=400 && out.upstream?.status<600 ? out.upstream.status : 502;
    return json(res,status,{ok:false,status:"BINANCE_UPSTREAM_REJECTED",upstream:out.upstream,tradingAction:"NONE"});
  }

  return json(res,200,{
    ok:true,
    status:"SIGNED_BINANCE_RELAY_OK",
    data:out.data,
    noSecretValuesExposed:true,
  });
}
