import { parseAndValidateCapability, sanitizeResponse } from "./relay-core.mjs";

const BASE=(Deno.env.get("BINANCE_PRIVATE_BASE_URL")||"https://api.binance.com").replace(/\/$/,"");
const WRITES_ENABLED = false;
const BINANCE_API_KEY_VAULT_NAME="binance_api_key_prod_v1";
const BINANCE_PRIVATE_KEY_VAULT_NAME="binance_ed25519_private_key_prod_v1";
const RELAY_PUBLIC_KEY_PEM=`-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAlaTKxorOgelDsnapk+ik3sFkDxJA5GdXbfZMBrFmjxA=
-----END PUBLIC KEY-----`;
const RELAY_AUTH_MAX_AGE_MS=15_000;
const RELAY_NONCE_TTL_MS=10*60_000;
const BINANCE_RECV_WINDOW=5000;

function b64ToBytes(value:string){
  const raw=atob(String(value||""));
  const out=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) out[i]=raw.charCodeAt(i);
  return out.buffer;
}

function publicKeyBytes(pem:string){
  const compact=String(pem||"")
    .replace(/-----BEGIN PUBLIC KEY-----/g,"")
    .replace(/-----END PUBLIC KEY-----/g,"")
    .replace(/\s+/g,"");
  return b64ToBytes(compact);
}

async function verifyEd25519(publicPem:string,message:string,signatureB64:string){
  const key=await crypto.subtle.importKey("spki",publicKeyBytes(publicPem),{name:"Ed25519"},false,["verify"]);
  return await crypto.subtle.verify("Ed25519",key,b64ToBytes(signatureB64),new TextEncoder().encode(message));
}

async function sha256Hex(value:string){
  const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(String(value||"")));
  return [...new Uint8Array(digest)].map((b)=>b.toString(16).padStart(2,"0")).join("");
}

function json(status:number,payload:unknown){
  return new Response(JSON.stringify(payload),{
    status,
    headers:{"content-type":"application/json","cache-control":"no-store"},
  });
}

function serviceAuthHeaders(){
  const serviceRole=String(Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")||"");
  if(!serviceRole) throw new Error("SUPABASE_SERVICE_ROLE_MISSING");
  return {
    "content-type":"application/json",
    "apikey":serviceRole,
    "authorization":`Bearer ${serviceRole}`,
  };
}
function supabaseUrl(){
  const u=String(Deno.env.get("SUPABASE_URL")||"").replace(/\/$/,"");
  if(!u) throw new Error("SUPABASE_URL_MISSING");
  return u;
}
async function restJson(url:string,init:RequestInit){
  const r=await fetch(url,{...init,signal:AbortSignal.timeout(8_000)});
  const text=await r.text();
  let data:any=null;
  try{data=text?JSON.parse(text):null;}catch{data=text;}
  return {r,data};
}

async function claimRelayNonce(nonce:string,expiresAt:number){
  const supabaseUrl=String(Deno.env.get("SUPABASE_URL")||"").replace(/\/$/,"");
  const serviceRole=String(Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")||"");
  if(!supabaseUrl||!serviceRole) throw new Error("RELAY_NONCE_STORE_NOT_CONFIGURED");
  const r=await fetch(`${supabaseUrl}/rest/v1/tst_relay_nonces`,{
    method:"POST",
    headers:{
      "content-type":"application/json",
      "apikey":serviceRole,
      "authorization":`Bearer ${serviceRole}`,
      "prefer":"return=minimal",
    },
    body:JSON.stringify({nonce,expires_at:new Date(expiresAt).toISOString()}),
    signal:AbortSignal.timeout(5_000),
  });
  if(r.status===409) return false;
  if(!r.ok) throw new Error("RELAY_NONCE_STORE_FAILED_"+r.status);
  return true;
}

async function verifyRelayAuth(body:any){
  const relayTimestamp=Number(body?.relayTimestamp);
  const relayNonce=String(body?.relayNonce||"");
  const relaySignature=String(body?.relaySignature||"");
  if(String(body?.apiKey||"")) throw new Error("CALLER_API_KEY_BLOCKED");
  if(!Number.isFinite(relayTimestamp)||Math.abs(Date.now()-relayTimestamp)>RELAY_AUTH_MAX_AGE_MS) throw new Error("RELAY_AUTH_STALE");
  if(!/^[A-Za-z0-9_-]{16,80}$/.test(relayNonce)) throw new Error("RELAY_NONCE_INVALID");
  if(!/^[A-Za-z0-9+/]{86}==$/.test(relaySignature)) throw new Error("RELAY_SIGNATURE_SHAPE_INVALID");
  const method=String(body?.method||"").toUpperCase();
  const path=String(body?.path||"");
  const query=String(body?.query||"");
  const apiKeyHash=await sha256Hex("");
  const queryHash=await sha256Hex(query);
  const canonical=[
    "TST_SUPABASE_RELAY_V2",
    String(relayTimestamp),
    relayNonce,
    method,
    path,
    apiKeyHash,
    queryHash,
  ].join("\n");
  if(!(await verifyEd25519(RELAY_PUBLIC_KEY_PEM,canonical,relaySignature))) throw new Error("RELAY_SIGNATURE_INVALID");
  if(!(await claimRelayNonce(relayNonce,Date.now()+RELAY_NONCE_TTL_MS))) throw new Error("RELAY_REPLAY_BLOCKED");
}


function decodeStatePayload(value:string){
  const raw=String(value||"").replace(/-/g,"+").replace(/_/g,"/");
  const padded=raw+"=".repeat((4-(raw.length%4))%4);
  const binary=atob(padded);
  const bytes=new Uint8Array(binary.length);
  for(let i=0;i<binary.length;i++) bytes[i]=binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

async function handleRuntimeState(body:any){
  try{
    const q=new URLSearchParams(String(body?.query||""));
    const op=String(q.get("op")||"").toLowerCase();
    const key=String(q.get("key")||"");
    if(!/^[A-Za-z0-9:_|.\/-]{1,300}$/.test(key)){
      return json(400,{ok:false,status:"STATE_BAD_KEY",financialAction:false});
    }
    const base=supabaseUrl();
    const headers=serviceAuthHeaders();

    if(op==="get"){
      const endpoint=new URL(base+"/rest/v1/tst_runtime_state");
      endpoint.searchParams.set("select","value");
      endpoint.searchParams.set("key","eq."+key);
      endpoint.searchParams.set("limit","1");
      const {r,data}=await restJson(endpoint.toString(),{method:"GET",headers});
      if(!r.ok) return json(503,{ok:false,status:"STATE_BACKEND_ERROR",reason:"REST_GET_"+r.status,financialAction:false});
      const row=Array.isArray(data)&&data.length?data[0]:null;
      return json(200,{ok:true,status:"STATE_GET_OK",found:Boolean(row),value:row?.value??null,financialAction:false});
    }

    if(op==="put" || op==="claim"){
      const expiresAt=Number(q.get("expiresAt")||0);
      const valueHash=String(q.get("valueHash")||"");
      const statePayload=String(body?.statePayload||"");
      if(!Number.isFinite(expiresAt)||expiresAt<=Date.now()||!/^[a-f0-9]{64}$/.test(valueHash)||!statePayload){
        return json(400,{ok:false,status:"STATE_BAD_WRITE_INPUT",financialAction:false});
      }
      if((await sha256Hex(statePayload))!==valueHash){
        return json(401,{ok:false,status:"STATE_VALUE_HASH_MISMATCH",financialAction:false});
      }
      let value:any;
      try{ value=JSON.parse(decodeStatePayload(statePayload)); }
      catch{ return json(400,{ok:false,status:"STATE_BAD_PAYLOAD",financialAction:false}); }

      if(op==="put"){
        const endpoint=new URL(base+"/rest/v1/tst_runtime_state");
        endpoint.searchParams.set("on_conflict","key");
        const putHeaders={...headers,"prefer":"resolution=merge-duplicates,return=minimal"};
        const {r}=await restJson(endpoint.toString(),{
          method:"POST",
          headers:putHeaders,
          body:JSON.stringify({key,value,expires_at:new Date(expiresAt).toISOString(),updated_at:new Date().toISOString()}),
        });
        if(!r.ok) return json(503,{ok:false,status:"STATE_BACKEND_ERROR",reason:"REST_PUT_"+r.status,financialAction:false});
        return json(200,{ok:true,status:"STATE_PUT_OK",financialAction:false});
      }

      const {r,data}=await restJson(base+"/rest/v1/rpc/tst_runtime_state_claim",{
        method:"POST",
        headers,
        body:JSON.stringify({p_key:key,p_value:value,p_expires_at:new Date(expiresAt).toISOString()}),
      });
      if(!r.ok) return json(503,{ok:false,status:"STATE_BACKEND_ERROR",reason:"RPC_CLAIM_"+r.status,financialAction:false});
      const claimed=data===true;
      return json(claimed?200:409,{ok:claimed,claimed,status:claimed?"STATE_CLAIMED":"STATE_ALREADY_CLAIMED",financialAction:false});
    }

    return json(400,{ok:false,status:"STATE_BAD_OPERATION",financialAction:false});
  }catch(error){
    return json(503,{ok:false,status:"STATE_BACKEND_ERROR",reason:String(error?.message||error).slice(0,200),financialAction:false});
  }
}

async function vaultSignerReady(){
  const envApiKey=Boolean(String(Deno.env.get("BINANCE_API_KEY")||"").trim());
  let privateKeyPresent=false;
  try{
    const {r,data}=await restJson(supabaseUrl()+"/rest/v1/rpc/tst_binance_ed25519_sign",{
      method:"POST",
      headers:serviceAuthHeaders(),
      body:JSON.stringify({p_payload:"tst-signer-health"}),
    });
    privateKeyPresent=r.ok && typeof data==="string" && data.length>40;
  }catch{}
  return {
    privateKeyPresent,
    apiKeyPresent:envApiKey,
    apiKeySource:envApiKey?"EDGE_FUNCTION_SECRET":"MISSING",
  };
}

async function getApiKey(){
  const envApiKey=String(Deno.env.get("BINANCE_API_KEY")||"").trim();
  if(!envApiKey) throw new Error("BINANCE_API_KEY_MISSING");
  return {apiKey:envApiKey,source:"EDGE_FUNCTION_SECRET"};
}

async function signWithVault(unsignedPayload:string){
  const {r,data}=await restJson(supabaseUrl()+"/rest/v1/rpc/tst_binance_ed25519_sign",{
    method:"POST",
    headers:serviceAuthHeaders(),
    body:JSON.stringify({p_payload:String(unsignedPayload)}),
  });
  if(!r.ok) throw new Error("BINANCE_SIGN_RPC_FAILED_"+r.status);
  const signature=String(data||"").replace(/\s+/g,"");
  if(!/^[A-Za-z0-9+/]{86}==$/.test(signature)) throw new Error("BINANCE_VAULT_SIGNATURE_INVALID");
  return signature;
}

function canonicalSignedQuery(unsignedQuery:string,timestamp:number){
  const q=new URLSearchParams(String(unsignedQuery||""));
  q.set("recvWindow",String(BINANCE_RECV_WINDOW));
  q.set("timestamp",String(Math.trunc(timestamp)));
  q.sort();
  return q.toString();
}

async function binanceTime(){
  const before=Date.now();
  const r=await fetch(`${BASE}/api/v3/time`,{
    method:"GET",
    headers:{"accept":"application/json","cache-control":"no-store"},
    signal:AbortSignal.timeout(10_000),
  });
  const after=Date.now();
  const data=await r.json().catch(()=>({}));
  const serverTime=Number(data?.serverTime||0);
  if(!r.ok||!Number.isFinite(serverTime)||serverTime<=0) throw new Error("BINANCE_SERVER_TIME_UNAVAILABLE");
  return {serverTime,roundTripMs:Math.max(0,after-before)};
}

Deno.serve(async(req:Request)=>{
  if(req.method==="GET"){
    const requestUrl=new URL(req.url);
    const probe=String(requestUrl.searchParams.get("probe")||"");
    const region=Deno.env.get("SB_REGION")||null;
    const baseMeta={
      region,
      financialAction:false,
      financialWritesEnabled:WRITES_ENABLED,
      signingMode:"ED25519_SUPABASE_VAULT",
      privateKeyLocation:"SUPABASE_VAULT",
      privateKeyExposed:false,
    };

    if(probe==="signerHealth"){
      try{
        const state=await vaultSignerReady();
        return json(200,{ok:state.privateKeyPresent&&state.apiKeyPresent,status:state.privateKeyPresent&&state.apiKeyPresent?"SUPABASE_VAULT_SIGNER_READY":"SUPABASE_VAULT_SIGNER_INCOMPLETE",...state,...baseMeta});
      }catch(error){
        return json(503,{ok:false,status:"SUPABASE_VAULT_SIGNER_ERROR",reason:String(error?.message||error).slice(0,160),...baseMeta});
      }
    }

    if(["public","time"].includes(probe)){
      try{
        const timing=await binanceTime();
        return json(200,{ok:true,status:"BINANCE_PUBLIC_CONNECTIVITY_OK",upstreamHttpStatus:200,serverTime:timing.serverTime,roundTripMs:timing.roundTripMs,...baseMeta});
      }catch(error){
        return json(200,{ok:false,status:"BINANCE_PUBLIC_CONNECTIVITY_ERROR",reason:String(error?.message||error).slice(0,160),...baseMeta});
      }
    }

    if(["exchangeInfo","bookTicker"].includes(probe)){
      const symbol=String(requestUrl.searchParams.get("symbol")||"").toUpperCase();
      if(!/^[A-Z0-9]{2,20}USDT$/.test(symbol)) return json(400,{ok:false,status:"BAD_PUBLIC_SYMBOL",...baseMeta});
      const endpoint=probe==="exchangeInfo"
        ? `/api/v3/exchangeInfo?symbol=${encodeURIComponent(symbol)}`
        : `/api/v3/ticker/bookTicker?symbol=${encodeURIComponent(symbol)}`;
      try{
        const upstream=await fetch(`${BASE}${endpoint}`,{
          method:"GET",
          headers:{"accept":"application/json","cache-control":"no-store"},
          signal:AbortSignal.timeout(10_000),
        });
        const data=await upstream.json().catch(()=>({}));
        const ok=upstream.ok&&!(typeof data?.code==="number"&&data.code<0);
        return json(ok?200:(upstream.status||502),{ok,status:ok?"BINANCE_PUBLIC_DATA_OK":"BINANCE_PUBLIC_DATA_REJECTED",upstreamHttpStatus:upstream.status,binanceCode:data?.code??null,data:ok?data:undefined,endpoint:probe,symbol,...baseMeta});
      }catch(error){
        return json(503,{ok:false,status:"BINANCE_PUBLIC_DATA_ERROR",reason:String(error?.message||error).slice(0,160),endpoint:probe,symbol,...baseMeta});
      }
    }

    return json(200,{ok:true,status:"SUPABASE_BINANCE_RELAY_READY",authMode:"CLOUDFLARE_ED25519_RELAY_AUTH",...baseMeta});
  }

  if(req.method!=="POST") return json(405,{ok:false,status:"METHOD_NOT_ALLOWED"});

  let body:any={};
  try{body=await req.json();}catch{return json(400,{ok:false,status:"BAD_JSON"});}

  try{await verifyRelayAuth(body);}
  catch(error){
    return json(401,{ok:false,status:String(error?.message||"RELAY_AUTH_REJECTED"),financialAction:false,privateKeyLocation:"SUPABASE_VAULT",privateKeyExposed:false});
  }

  if(String(body?.method||"").toUpperCase()==="POST" && String(body?.path||"")==="/internal/state"){
    return await handleRuntimeState(body);
  }

  let cap:any;
  try{cap=parseAndValidateCapability(body,{writesEnabled:WRITES_ENABLED});}
  catch(error){
    return json(403,{ok:false,status:String(error?.message||"CAPABILITY_REJECTED"),financialAction:false,privateKeyLocation:"SUPABASE_VAULT",privateKeyExposed:false});
  }

  let apiKeyInfo:any;
  let timing:any;
  let unsignedPayload="";
  let signature="";
  try{
    [apiKeyInfo,timing]=await Promise.all([getApiKey(),binanceTime()]);
    unsignedPayload=canonicalSignedQuery(cap.unsignedQuery,timing.serverTime);
    signature=await signWithVault(unsignedPayload);
  }catch(error){
    return json(503,{
      ok:false,
      status:"BINANCE_SIGNING_PREP_FAILED",
      reason:String(error?.message||error).slice(0,160),
      financialAction:false,
      noRequestSent:true,
      privateKeyLocation:"SUPABASE_VAULT",
      privateKeyExposed:false,
    });
  }

  const signedQuery=unsignedPayload+"&signature="+encodeURIComponent(signature);
  const url=`${BASE}${cap.path}?${signedQuery}`;
  const startedAt=Date.now();
  try{
    const upstream=await fetch(url,{
      method:cap.method,
      headers:{
        "X-MBX-APIKEY":apiKeyInfo.apiKey,
        "accept":"application/json",
        "cache-control":"no-store",
      },
      signal:AbortSignal.timeout(15_000),
    });
    const raw=await upstream.text();
    let data:any={};
    try{data=raw?JSON.parse(raw):{};}catch{data={raw:raw.slice(0,300)};}
    const ok=upstream.ok&&!(typeof data?.code==="number"&&data.code<0);
    return json(ok?200:(upstream.status||502),{
      ok,
      status:ok?"BINANCE_RELAY_OK":"BINANCE_RELAY_REJECTED",
      upstreamHttpStatus:upstream.status,
      binanceCode:data?.code??null,
      data:ok?sanitizeResponse(cap.path,cap.method,data):undefined,
      message:ok?undefined:String(data?.msg||data?.raw||"upstream rejected").slice(0,180),
      financialAction:cap.isWrite,
      dryRun:cap.isTest===true,
      latencyMs:Date.now()-startedAt,
      diagnostics:{
        endpoint:cap.path,
        method:cap.method,
        unsignedCanonicalPayload:String(unsignedPayload).slice(0,2000),
        timestampMs:timing.serverTime,
        serverTimeMs:timing.serverTime,
        timestampDeltaMs:0,
        recvWindow:BINANCE_RECV_WINDOW,
        httpStatus:upstream.status,
        binanceCode:data?.code??null,
        signatureType:"ED25519",
        apiKeySource:apiKeyInfo.source,
        privateKeyLocation:"SUPABASE_VAULT",
        secretExposed:false,
      },
      privateKeyLocation:"SUPABASE_VAULT",
      privateKeyExposed:false,
    });
  }catch(error){
    return json(503,{
      ok:false,
      status:"BINANCE_TRANSPORT_UNKNOWN",
      reason:String(error?.name||"FetchError"),
      reconciliationRequired:cap?.isWrite===true,
      mayResend:false,
      financialAction:cap?.isWrite===true,
      latencyMs:Date.now()-startedAt,
      privateKeyLocation:"SUPABASE_VAULT",
      privateKeyExposed:false,
    });
  }
});
