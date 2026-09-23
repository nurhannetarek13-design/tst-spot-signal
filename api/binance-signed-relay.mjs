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

function validSignedQuery(query) {
  if (typeof query!=="string" || query.length<20 || query.length>4096) return false;
  const q=new URLSearchParams(query);
  const timestamp=Number(q.get("timestamp"));
  const recvWindow=Number(q.get("recvWindow")||5000);
  const signature=String(q.get("signature")||"");
  if (!Number.isFinite(timestamp) || Math.abs(Date.now()-timestamp)>Math.max(60_000,recvWindow+10_000)) return false;
  if (!Number.isFinite(recvWindow) || recvWindow<1 || recvWindow>60_000) return false;
  return /^[a-f0-9]{64}$/i.test(signature);
}

async function binanceForward(method,path,apiKey,query) {
  let last={status:502,code:null,msg:"Binance unavailable"};
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
      last={status:r.status,code:data?.code??null,msg:String(data?.msg||"upstream rejected").slice(0,160)};
      if (Number(last.code)<0) break;
    } catch(e) {
      last={status:502,code:null,msg:String(e?.message||e).slice(0,160)};
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

  let body={}; try { body=typeof req.body==="object" ? req.body : JSON.parse(raw||"{}"); } catch {}
  const method=String(body.method||"").toUpperCase();
  const path=String(body.path||"");
  const apiKey=String(body.apiKey||"");
  const query=String(body.query||"");
  const network=String(body.network||"");
  const op=`${method} ${path}`;

  if (network!=="production") return res.status(400).json({ok:false,status:"PRODUCTION_ONLY"});
  if (!ALLOWED.has(op)) return res.status(403).json({ok:false,status:"OPERATION_NOT_ALLOWED",tradingAction:"NONE"});
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey) || !validSignedQuery(query)) {
    return res.status(400).json({ok:false,status:"BAD_SIGNED_REQUEST",tradingAction:"NONE"});
  }

  const out=await binanceForward(method,path,apiKey,query);
  if (!out.ok) return res.status(out.upstream?.status>=400&&out.upstream?.status<600?out.upstream.status:502)
    .json({ok:false,status:"BINANCE_UPSTREAM_REJECTED",upstream:out.upstream});

  return res.status(200).json({
    ok:true,
    status:"SIGNED_BINANCE_RELAY_OK",
    data:out.data,
    noSecretValuesExposed:true,
  });
}
