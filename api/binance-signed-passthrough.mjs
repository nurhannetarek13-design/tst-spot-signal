import crypto from "node:crypto";
import { validateSignedOperation } from "../lib/binance-signed-passthrough-policy.mjs";

const BINANCE_GET_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];
const BINANCE_WRITE_BASE = "https://api.binance.com";

function json(res,status,body) {
  res.setHeader("Cache-Control","no-store");
  return res.status(status).json(body);
}
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
async function parseResponse(r) {
  const text=await r.text();
  let data={}; try { data=JSON.parse(text||"{}"); } catch {}
  if (r.ok && !(Number(data?.code)<0)) return {ok:true,data};
  return {ok:false,upstream:{status:r.status,code:data?.code??null}};
}
async function forwardRead(path,apiKey,query) {
  let last={status:502,code:null};
  for (const base of BINANCE_GET_BASES) {
    try {
      const r=await fetch(`${base}${path}?${query}`,{
        method:"GET",
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const out=await parseResponse(r);
      if (out.ok) return out;
      last=out.upstream;
      if (Number(last.code)<0) break;
    } catch {
      last={status:502,code:null};
    }
  }
  return {ok:false,upstream:last};
}
async function forwardWrite(path,apiKey,query) {
  try {
    const r=await fetch(`${BINANCE_WRITE_BASE}${path}?${query}`,{
      method:"POST",
      headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
      signal:AbortSignal.timeout(12_000),
    });
    return await parseResponse(r);
  } catch {
    // Critical: an unknown POST result is never retried on another Binance host.
    return {ok:false,upstream:{status:502,code:null,transport:"WRITE_TRANSPORT_STATUS_UNKNOWN"}};
  }
}
function diagnostic(upstream) {
  const code=Number(upstream?.code);
  const status=Number(upstream?.status);
  if (code===-1022) return "BINANCE_SIGNATURE_REJECTED";
  if (code===-2015) return "BINANCE_CREDENTIAL_OR_IP_REJECTED";
  if (code===-1021) return "BINANCE_CLOCK_REJECTED";
  if (status===451) return "BINANCE_REGION_RESTRICTED_HTTP_451";
  if (status===403) return "BINANCE_HTTP_403";
  if (status===429) return "BINANCE_RATE_LIMIT_HTTP_429";
  if (status>=500) return upstream?.transport||"BINANCE_OR_NETWORK_5XX";
  return code ? `BINANCE_CODE_${code}` : `BINANCE_HTTP_${status||"UNKNOWN"}`;
}

export default async function handler(req,res) {
  if (req.method!=="POST") return json(res,405,{ok:false,status:"METHOD_NOT_ALLOWED",tradingAction:"NONE"});

  const raw=typeof req.body==="string" ? req.body : JSON.stringify(req.body||{});
  const auth=verifyEnvelope(req,raw);
  if (!auth.ok) return json(res,401,{...auth,tradingAction:"NONE"});

  let body={};
  try { body=typeof req.body==="object" ? req.body : JSON.parse(raw||"{}"); }
  catch { return json(res,400,{ok:false,status:"BAD_JSON",tradingAction:"NONE"}); }

  const keys=Object.keys(body).sort();
  const expected=["apiKey","method","network","path","query"].sort();
  if (keys.length!==expected.length || !keys.every((k,i)=>k===expected[i])) {
    return json(res,400,{ok:false,status:"RELAY_BODY_NOT_ALLOWED",tradingAction:"NONE"});
  }
  if (String(body.network)!=="production") return json(res,400,{ok:false,status:"PRODUCTION_ONLY",tradingAction:"NONE"});

  const apiKey=String(body.apiKey||"").trim();
  const method=String(body.method||"").toUpperCase();
  const path=String(body.path||"");
  const query=String(body.query||"");
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)) {
    return json(res,400,{ok:false,status:"BAD_API_KEY_SHAPE",tradingAction:"NONE"});
  }

  const checked=validateSignedOperation(method,path,query);
  if (!checked.ok) return json(res,403,{ok:false,status:checked.status,tradingAction:"NONE"});

  const out=method==="GET"
    ? await forwardRead(path,apiKey,query)
    : await forwardWrite(path,apiKey,query);

  if (!out.ok) {
    const status=out.upstream?.status>=400&&out.upstream?.status<600 ? out.upstream.status : 502;
    return json(res,status,{
      ok:false,
      status:"BINANCE_UPSTREAM_REJECTED",
      diagnosticCode:diagnostic(out.upstream),
      safeHttpStatus:out.upstream?.status??null,
      safeBinanceCode:out.upstream?.code??null,
      operationKind:checked.kind,
      tradingAction:"NONE_CONFIRMED",
      noSecretReceived:true,
    });
  }

  return json(res,200,{
    ok:true,
    status:"CLOUDFLARE_SIGNED_BINANCE_PASSTHROUGH_OK",
    operationKind:checked.kind,
    data:out.data,
    noSecretReceived:true,
  });
}
