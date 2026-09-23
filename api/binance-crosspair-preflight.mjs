import crypto from "node:crypto";

const BINANCE_BASES=[
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function safeEqualHex(a,b){
  const aa=String(a||"").toLowerCase(),bb=String(b||"").toLowerCase();
  if(!/^[a-f0-9]{64}$/.test(aa)||!/^[a-f0-9]{64}$/.test(bb)) return false;
  return crypto.timingSafeEqual(Buffer.from(aa,"hex"),Buffer.from(bb,"hex"));
}
function verifyEnvelope(req,raw){
  const secret=String(process.env.TELEGRAM_BOT_TOKEN||"").trim();
  const ts=String(req.headers["x-executor-timestamp"]||"");
  const sig=String(req.headers["x-executor-signature"]||"");
  const stamp=Number(ts);
  if(!secret) return {ok:false,status:"RELAY_SECRET_MISSING"};
  if(!Number.isFinite(stamp)||Math.abs(Date.now()-stamp)>60_000) return {ok:false,status:"STALE_RELAY_REQUEST"};
  const expected=crypto.createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");
  return safeEqualHex(expected,sig)?{ok:true}:{ok:false,status:"BAD_RELAY_SIGNATURE"};
}
function normalize(v){return String(v||"").trim();}
function signHmac(secret,params){
  const q=new URLSearchParams();
  for(const [k,v] of Object.entries(params)) q.append(k,String(v));
  const unsigned=q.toString();
  const signature=crypto.createHmac("sha256",secret).update(unsigned).digest("hex");
  q.append("signature",signature);
  return q.toString();
}
function validateCloudflareSignedQuery(query){
  if(typeof query!=="string"||query.length<70||query.length>1024) return false;
  const q=new URLSearchParams(query);
  const keys=[...q.keys()].sort();
  const expected=["recvWindow","signature","timestamp"].sort();
  if(keys.length!==expected.length||!keys.every((k,i)=>k===expected[i])) return false;
  const ts=Number(q.get("timestamp")),recv=Number(q.get("recvWindow"));
  const sig=String(q.get("signature")||"");
  return Number.isFinite(ts)&&Math.abs(Date.now()-ts)<=60_000&&
    Number.isFinite(recv)&&recv>=1&&recv<=60_000&&
    /^[A-Za-z0-9+/=_%-]{64,1024}$/.test(sig);
}
async function forward(apiKey,query){
  let last={status:502,code:null};
  for(const base of BINANCE_BASES){
    try{
      const r=await fetch(`${base}/api/v3/account?${query}`,{
        method:"GET",
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const txt=await r.text();
      let data={};try{data=JSON.parse(txt||"{}");}catch{}
      if(r.ok&&!(Number(data?.code)<0)) return {ok:true,canTrade:Boolean(data.canTrade)};
      last={status:r.status,code:data?.code??null};
      if(Number(last.code)<0) break;
    }catch{last={status:502,code:null};}
  }
  return {ok:false,...last};
}
function diagnostic(out){
  if(out.ok) return "ACCOUNT_OK";
  if(out.code===-1022) return "BINANCE_SIGNATURE_REJECTED";
  if(out.code===-2015) return "BINANCE_CREDENTIAL_OR_IP_REJECTED";
  if(out.code===-1021) return "BINANCE_CLOCK_REJECTED";
  if(out.status===451) return "BINANCE_REGION_RESTRICTED_HTTP_451";
  return out.code!=null?`BINANCE_CODE_${out.code}`:`BINANCE_HTTP_${out.status}`;
}

export default async function handler(req,res){
  res.setHeader("Cache-Control","no-store");
  if(req.method!=="POST") return res.status(405).json({ok:false,status:"METHOD_NOT_ALLOWED"});
  const raw=typeof req.body==="string"?req.body:JSON.stringify(req.body||{});
  const auth=verifyEnvelope(req,raw);
  if(!auth.ok) return res.status(401).json({...auth,tradingAction:"NONE"});
  let body={};try{body=typeof req.body==="object"?req.body:JSON.parse(raw||"{}");}
  catch{return res.status(400).json({ok:false,status:"BAD_JSON",tradingAction:"NONE"});}

  const mode=String(body.mode||"");
  let apiKey="",query="";
  if(mode==="CF_KEY_VERCEL_SECRET"){
    apiKey=String(body.cloudflareApiKey||"").trim();
    const secret=normalize(process.env.BINANCE_API_SECRET);
    if(!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)||!secret){
      return res.status(400).json({ok:false,status:"CROSSPAIR_INPUT_MISSING",tradingAction:"NONE"});
    }
    query=signHmac(secret,{recvWindow:"5000",timestamp:String(Date.now())});
  }else if(mode==="VERCEL_KEY_CF_SECRET"){
    apiKey=normalize(process.env.BINANCE_API_KEY);
    query=String(body.cloudflareSignedQuery||"");
    if(!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)||!validateCloudflareSignedQuery(query)){
      return res.status(400).json({ok:false,status:"CROSSPAIR_INPUT_MISSING",tradingAction:"NONE"});
    }
  }else{
    return res.status(400).json({ok:false,status:"MODE_NOT_ALLOWED",tradingAction:"NONE"});
  }

  const out=await forward(apiKey,query);
  return res.status(200).json({
    ok:out.ok===true,
    canTrade:out.ok===true?out.canTrade===true:false,
    mode,
    diagnosticCode:diagnostic(out),
    safeHttpStatus:out.status??200,
    safeBinanceCode:out.code??null,
    tradingAction:"NONE",
    noSecretValuesExposed:true,
    noBalanceValuesExposed:true,
  });
}
