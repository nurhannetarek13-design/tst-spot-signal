import crypto from "node:crypto";

const BINANCE_GET_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function safeEqualHex(a,b) {
  const aa=String(a||"").toLowerCase(), bb=String(b||"").toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(aa) || !/^[a-f0-9]{64}$/.test(bb)) return false;
  return crypto.timingSafeEqual(Buffer.from(aa,"hex"),Buffer.from(bb,"hex"));
}

function verifyEnvelope(req,raw) {
  const secret=String(process.env.TELEGRAM_BOT_TOKEN||"").trim();
  if (!secret) return {ok:false,status:"RELAY_SECRET_MISSING"};
  const ts=String(req.headers["x-executor-timestamp"]||"");
  const sig=String(req.headers["x-executor-signature"]||"");
  const stamp=Number(ts);
  if (!Number.isFinite(stamp) || Math.abs(Date.now()-stamp)>60_000) return {ok:false,status:"STALE_RELAY_REQUEST"};
  const expected=crypto.createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");
  return safeEqualHex(expected,sig) ? {ok:true} : {ok:false,status:"BAD_RELAY_SIGNATURE"};
}

function validateSignedQuery(query) {
  if (typeof query!=="string" || query.length<80 || query.length>2000) return {ok:false,status:"BAD_SIGNED_QUERY"};
  const q=new URLSearchParams(query);
  const keys=[...q.keys()].sort();
  const allowed=["recvWindow","signature","timestamp"].sort();
  if (keys.length!==allowed.length || !keys.every((k,i)=>k===allowed[i])) return {ok:false,status:"ACCOUNT_QUERY_SHAPE_NOT_ALLOWED"};
  const stamp=Number(q.get("timestamp"));
  const recv=Number(q.get("recvWindow"));
  const sig=String(q.get("signature")||"");
  if (!Number.isFinite(stamp) || Math.abs(Date.now()-stamp)>60_000) return {ok:false,status:"STALE_BINANCE_QUERY"};
  if (!Number.isFinite(recv) || recv<1 || recv>60_000) return {ok:false,status:"BAD_RECV_WINDOW"};
  if (!(/^[a-f0-9]{64}$/i.test(sig) || /^[A-Za-z0-9+/=]{80,1024}$/.test(sig))) return {ok:false,status:"BAD_BINANCE_SIGNATURE_SHAPE"};
  return {ok:true};
}

async function forward(apiKey,query) {
  let last={status:502,code:null};
  for (const base of BINANCE_GET_BASES) {
    try {
      const r=await fetch(`${base}/api/v3/account?${query}`,{
        method:"GET",
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const text=await r.text();
      let row={}; try { row=JSON.parse(text||"{}"); } catch {}
      if (r.ok && !(Number(row?.code)<0)) {
        return {ok:true,data:{canTrade:Boolean(row.canTrade),accountType:row.accountType||null}};
      }
      last={status:r.status,code:row?.code??null};
      if (Number(last.code)<0) break;
    } catch {
      last={status:502,code:null};
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

  if (Object.keys(body).sort().join(",")!=="apiKey,network,query") {
    return res.status(400).json({ok:false,status:"RELAY_BODY_NOT_ALLOWED",tradingAction:"NONE"});
  }
  if (String(body.network)!=="production") return res.status(400).json({ok:false,status:"PRODUCTION_ONLY",tradingAction:"NONE"});
  const apiKey=String(body.apiKey||"").trim();
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)) return res.status(400).json({ok:false,status:"BAD_API_KEY_SHAPE",tradingAction:"NONE"});
  const checked=validateSignedQuery(String(body.query||""));
  if (!checked.ok) return res.status(400).json({...checked,tradingAction:"NONE"});

  const out=await forward(apiKey,String(body.query));
  if (!out.ok) {
    const status=out.upstream?.status>=400&&out.upstream?.status<600?out.upstream.status:502;
    return res.status(status).json({
      ok:false,
      status:"BINANCE_UPSTREAM_REJECTED",
      upstream:{status:out.upstream?.status??null,code:out.upstream?.code??null},
      tradingAction:"NONE",
      noSecretValuesExposed:true,
    });
  }
  return res.status(200).json({
    ok:true,
    status:"CLOUDFLARE_SIGNED_ACCOUNT_OK",
    canTrade:out.data.canTrade,
    accountType:out.data.accountType,
    tradingAction:"NONE",
    noBalanceValuesExposed:true,
    noSecretValuesExposed:true,
  });
}
