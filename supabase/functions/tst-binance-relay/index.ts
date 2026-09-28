import { parseAndValidateCapability, sanitizeResponse } from "./relay-core.mjs";

const BASE = (Deno.env.get("BINANCE_PRIVATE_BASE_URL") || "https://api.binance.com").replace(/\/$/, "");
// First deployment is deliberately read-only. Promotion to writes is a code-reviewed change.
const WRITES_ENABLED = false;
const RELAY_PUBLIC_KEY_PEM = `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAlaTKxorOgelDsnapk+ik3sFkDxJA5GdXbfZMBrFmjxA=
-----END PUBLIC KEY-----`;
const RELAY_AUTH_MAX_AGE_MS = 15_000;
const RELAY_NONCE_TTL_MS = 10 * 60_000;

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

async function verifyEd25519(publicPem:string, message:string, signatureB64:string){
  const key=await crypto.subtle.importKey(
    "spki",
    publicKeyBytes(publicPem),
    {name:"Ed25519"},
    false,
    ["verify"],
  );
  return await crypto.subtle.verify(
    "Ed25519",
    key,
    b64ToBytes(signatureB64),
    new TextEncoder().encode(message),
  );
}

async function claimRelayNonce(nonce:string, expiresAt:number){
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
  if(!Number.isFinite(relayTimestamp)||Math.abs(Date.now()-relayTimestamp)>RELAY_AUTH_MAX_AGE_MS){
    throw new Error("RELAY_AUTH_STALE");
  }
  if(!/^[A-Za-z0-9_-]{16,80}$/.test(relayNonce)) throw new Error("RELAY_NONCE_INVALID");
  if(!/^[A-Za-z0-9+/]{86}==$/.test(relaySignature)) throw new Error("RELAY_SIGNATURE_SHAPE_INVALID");
  const method=String(body?.method||"").toUpperCase();
  const path=String(body?.path||"");
  const apiKey=String(body?.apiKey||"");
  const query=String(body?.query||"");
  const apiKeyHash=await sha256Hex(apiKey);
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
  const verified=await verifyEd25519(RELAY_PUBLIC_KEY_PEM,canonical,relaySignature);
  if(!verified) throw new Error("RELAY_SIGNATURE_INVALID");
  const claimed=await claimRelayNonce(relayNonce,Date.now()+RELAY_NONCE_TTL_MS);
  if(!claimed) throw new Error("RELAY_REPLAY_BLOCKED");
  return {ok:true,relayTimestamp,relayNonce};
}


async function sha256Hex(value:string){
  const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(String(value||"")));
  return [...new Uint8Array(digest)].map((b)=>b.toString(16).padStart(2,"0")).join("");
}

function json(status:number, payload:unknown){
  return new Response(JSON.stringify(payload), {
    status,
    headers:{
      "content-type":"application/json",
      "cache-control":"no-store",
    },
  });
}

Deno.serve(async (req:Request) => {
  if(req.method === "GET"){
    const requestUrl = new URL(req.url);
    if(["public","time"].includes(requestUrl.searchParams.get("probe") || "")){
      try{
        const upstream = await fetch(`${BASE}/api/v3/time`, {
          method:"GET",
          headers:{"accept":"application/json","cache-control":"no-store"},
          signal:AbortSignal.timeout(10_000),
        });
        const data = await upstream.json().catch(()=>({}));
        return json(200,{
          ok:upstream.ok,
          status:upstream.ok?"BINANCE_PUBLIC_CONNECTIVITY_OK":"BINANCE_PUBLIC_CONNECTIVITY_BLOCKED",
          upstreamHttpStatus:upstream.status,
          binanceCode:data?.code ?? null,
          serverTime:Number(data?.serverTime || 0) || null,
          region:Deno.env.get("SB_REGION") || null,
          financialAction:false,
          financialWritesEnabled:WRITES_ENABLED,
          noBinanceSecretStored:true,
        });
      }catch(error){
        return json(200,{
          ok:false,
          status:"BINANCE_PUBLIC_CONNECTIVITY_ERROR",
          reason:String(error?.name || "FetchError"),
          region:Deno.env.get("SB_REGION") || null,
          financialAction:false,
          financialWritesEnabled:WRITES_ENABLED,
          noBinanceSecretStored:true,
        });
      }
    }
    return json(200,{
      ok:true,
      status:"SUPABASE_BINANCE_RELAY_READY",
      financialWritesEnabled:WRITES_ENABLED,
      authMode:"BINANCE_SIGNED_CAPABILITY",
      region:Deno.env.get("SB_REGION") || null,
      noBinanceSecretStored:true,
    });
  }

  if(req.method !== "POST"){
    return json(405,{ok:false,status:"METHOD_NOT_ALLOWED"});
  }

  let body:any={};
  try{ body=await req.json(); }
  catch{ return json(400,{ok:false,status:"BAD_JSON"}); }

  try{
    await verifyRelayAuth(body);
  }catch(error){
    return json(401,{
      ok:false,
      status:String(error?.message || "RELAY_AUTH_REJECTED"),
      financialAction:false,
      noBinanceSecretStored:true,
    });
  }

  let cap;
  try{
    cap=parseAndValidateCapability(body,{writesEnabled:WRITES_ENABLED});
  }catch(error){
    return json(403,{
      ok:false,
      status:String(error?.message || "CAPABILITY_REJECTED"),
      financialAction:false,
    });
  }

  const url=`${BASE}${cap.path}?${cap.query}`;
  try{
    const upstream=await fetch(url,{
      method:cap.method,
      headers:{
        "X-MBX-APIKEY":cap.apiKey,
        "accept":"application/json",
        "cache-control":"no-store",
      },
      signal:AbortSignal.timeout(15_000),
    });

    const raw=await upstream.text();
    let data:any={};
    try{ data=raw?JSON.parse(raw):{}; }catch{ data={raw:raw.slice(0,300)}; }

    const ok=upstream.ok && !(typeof data?.code === "number" && data.code < 0);
    return json(ok?200:(upstream.status||502),{
      ok,
      status:ok?"BINANCE_RELAY_OK":"BINANCE_RELAY_REJECTED",
      upstreamHttpStatus:upstream.status,
      binanceCode:data?.code ?? null,
      data:ok?sanitizeResponse(cap.path,cap.method,data):undefined,
      message:ok?undefined:String(data?.msg || data?.raw || "upstream rejected").slice(0,180),
      financialAction:cap.isWrite,
      dryRun:cap.isTest === true,
      diagnostics:{
        endpoint:cap.path,
        method:cap.method,
        unsignedCanonicalPayload:String(cap.unsignedQuery || "").slice(0,2000),
        timestampMs:Number(cap.timestamp || 0) || null,
        timestampDeltaMs:Number(cap.timestampDeltaMs || 0),
        recvWindow:Number(cap.recvWindow || 0),
        httpStatus:upstream.status,
        binanceCode:data?.code ?? null,
        signatureType:cap.signatureType || null,
        secretExposed:false,
      },
      noBinanceSecretStored:true,
    });
  }catch(error){
    return json(503,{
      ok:false,
      status:"BINANCE_TRANSPORT_UNKNOWN",
      reason:String(error?.name || "FetchError"),
      reconciliationRequired:cap?.isWrite === true,
      mayResend:false,
      financialAction:cap?.isWrite === true,
      noBinanceSecretStored:true,
    });
  }
});
