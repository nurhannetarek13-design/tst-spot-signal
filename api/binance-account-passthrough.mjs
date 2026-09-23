import crypto from "node:crypto";

const BINANCE_BASES = [
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
  if (!Number.isFinite(stamp) || Math.abs(Date.now()-stamp)>60_000) {
    return {ok:false,status:"STALE_RELAY_REQUEST"};
  }
  const expected=crypto.createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");
  return safeEqualHex(expected,sig) ? {ok:true} : {ok:false,status:"BAD_RELAY_SIGNATURE"};
}

function validSignedAccountQuery(query) {
  if (typeof query!=="string" || query.length<70 || query.length>1024) return false;
  const q=new URLSearchParams(query);
  const allowed=[...q.keys()].sort();
  const expected=["recvWindow","signature","timestamp"].sort();
  if (allowed.length!==expected.length || !allowed.every((k,i)=>k===expected[i])) return false;
  const ts=Number(q.get("timestamp"));
  const recv=Number(q.get("recvWindow"));
  const sig=String(q.get("signature")||"");
  if (!Number.isFinite(ts) || Math.abs(Date.now()-ts)>60_000) return false;
  if (!Number.isFinite(recv) || recv<1 || recv>60_000) return false;
  return /^[A-Za-z0-9+/=_%-]{64,1024}$/.test(sig);
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

  const apiKey=String(body.apiKey||"").trim();
  const query=String(body.query||"");
  if (!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey) || !validSignedAccountQuery(query)) {
    return res.status(400).json({ok:false,status:"BAD_READONLY_REQUEST",tradingAction:"NONE"});
  }

  let last={status:502,code:null};
  for (const base of BINANCE_BASES) {
    try {
      const r=await fetch(`${base}/api/v3/account?${query}`,{
        method:"GET",
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const txt=await r.text();
      let data={}; try { data=JSON.parse(txt||"{}"); } catch {}
      if (r.ok && !(Number(data?.code)<0)) {
        return res.status(200).json({
          ok:true,
          status:"SIGNED_ACCOUNT_PASSTHROUGH_OK",
          data,
          tradingAction:"NONE",
          noSecretReceived:true,
        });
      }
      last={status:r.status,code:data?.code??null};
      if (Number(last.code)<0) break;
    } catch {
      last={status:502,code:null};
    }
  }

  const diagnostic = last.code===-1022 ? "BINANCE_SIGNATURE_REJECTED"
    : last.code===-2015 ? "BINANCE_CREDENTIAL_OR_IP_REJECTED"
    : last.code===-1021 ? "BINANCE_CLOCK_REJECTED"
    : last.status===451 ? "BINANCE_REGION_RESTRICTED_HTTP_451"
    : last.status===403 ? "BINANCE_HTTP_403"
    : last.status>=500 ? "BINANCE_OR_NETWORK_5XX"
    : last.code!=null ? `BINANCE_CODE_${last.code}`
    : `BINANCE_HTTP_${last.status}`;

  return res.status(last.status>=400&&last.status<600?last.status:502).json({
    ok:false,
    status:"BINANCE_ACCOUNT_PASSTHROUGH_FAILED",
    diagnosticCode:diagnostic,
    safeHttpStatus:last.status,
    safeBinanceCode:last.code,
    tradingAction:"NONE",
    noSecretReceived:true,
  });
}
