import crypto from "node:crypto";

// Preview redeploy probe: read-only transport only; financial writes remain disabled.
const BINANCE_BASES = [
  "https://api.binance.com",
  "https://api-gcp.binance.com",
  "https://api1.binance.com",
  "https://api2.binance.com",
  "https://api3.binance.com",
  "https://api4.binance.com",
];

function apiKeyFingerprint(apiKey){
  return crypto.createHash("sha256").update(String(apiKey||"")).digest("hex");
}

function validateSignedQuery(query){
  if(typeof query!=="string"||query.length<20||query.length>4096) return false;
  const q=new URLSearchParams(query);
  const timestamp=Number(q.get("timestamp"));
  const recvWindow=Number(q.get("recvWindow")||5000);
  const signature=String(q.get("signature")||"");
  if(!Number.isFinite(timestamp)||Math.abs(Date.now()-timestamp)>Math.max(60_000,recvWindow+10_000)) return false;
  if(!Number.isFinite(recvWindow)||recvWindow<1||recvWindow>60_000) return false;
  return /^[a-f0-9]{64}$/i.test(signature);
}

async function readOnlyForward(path,apiKey,query){
  let last={status:502,code:null,msg:"Binance unavailable"};
  for(const base of BINANCE_BASES){
    try{
      const r=await fetch(`${base}${path}?${query}`,{
        method:"GET",
        headers:{"X-MBX-APIKEY":apiKey,"Accept":"application/json","Cache-Control":"no-store"},
        signal:AbortSignal.timeout(12_000),
      });
      const text=await r.text();
      let data={};
      try{data=JSON.parse(text||"{}");}catch{data={};}
      if(r.ok&&!(Number(data?.code)<0)) return {ok:true,data};
      last={status:r.status,code:data?.code??null,msg:String(data?.msg||"upstream rejected").slice(0,160)};
      if(Number(last.code)<0) break;
    }catch(e){
      last={status:502,code:null,msg:String(e?.message||e).slice(0,160)};
    }
  }
  return {ok:false,upstream:last};
}

export default async function handler(req,res){
  res.setHeader("cache-control","no-store");

  if(req.method==="GET"){
    return res.status(200).json({
      ok:true,
      status:"VERCEL_TRANSPORT_V2_READONLY_READY",
      authMode:"BINANCE_SIGNED_CAPABILITY",
      relaySecretRequired:false,
      financialWritesEnabled:false,
      noSecretValuesExposed:true,
    });
  }

  if(req.method!=="POST"){
    return res.status(405).json({ok:false,status:"METHOD_NOT_ALLOWED"});
  }

  const raw=typeof req.body==="string"?req.body:JSON.stringify(req.body||{});
  let body={};
  try{body=typeof req.body==="object"?req.body:JSON.parse(raw||"{}");}catch{}
  const method=String(body.method||"").toUpperCase();
  const path=String(body.path||"");
  const apiKey=String(body.apiKey||"");
  const query=String(body.query||"");
  const network=String(body.network||"");

  if(network!=="production") return res.status(400).json({ok:false,status:"PRODUCTION_ONLY",financialAction:false});
  if(method!=="GET"||path!=="/api/v3/account"){
    return res.status(403).json({ok:false,status:"READ_ONLY_ROUTE",financialAction:false});
  }
  if(!/^[A-Za-z0-9_-]{20,256}$/.test(apiKey)||!validateSignedQuery(query)){
    return res.status(400).json({ok:false,status:"BAD_SIGNED_REQUEST",financialAction:false});
  }

  const out=await readOnlyForward(path,apiKey,query);
  if(!out.ok){
    const status=out.upstream?.status>=400&&out.upstream?.status<600?out.upstream.status:502;
    return res.status(status).json({
      ok:false,
      status:"BINANCE_READONLY_UPSTREAM_REJECTED",
      upstream:out.upstream,
      financialAction:false,
    });
  }

  return res.status(200).json({
    ok:true,
    status:"BINANCE_READONLY_RELAY_OK",
    data:{
      canTrade:Boolean(out.data?.canTrade),
      accountType:out.data?.accountType,
    },
    financialAction:false,
    noBalanceValuesExposed:true,
    noSecretValuesExposed:true,
  });
}
