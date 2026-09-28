import { signRelayEnvelope } from "./binance-signing.js";

function relayUrl(env={}){
  return String(env.SUPABASE_BINANCE_RELAY_URL||"").trim();
}
function relayPrivateKey(env={}){
  return String(env.BINANCE_ED25519_PRIVATE_KEY||"").trim();
}
function relayRegion(env={}){
  return String(env.SUPABASE_BINANCE_REGION||"eu-west-1").trim()||"eu-west-1";
}
function randomNonce(){
  const bytes=new Uint8Array(18);
  crypto.getRandomValues(bytes);
  let raw="";
  for(const b of bytes) raw+=String.fromCharCode(b);
  return btoa(raw).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/g,"");
}
async function sha256Hex(text){
  const d=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(String(text||"")));
  return [...new Uint8Array(d)].map(b=>b.toString(16).padStart(2,"0")).join("");
}
function encodePayload(value){
  const text=JSON.stringify(value);
  const bytes=new TextEncoder().encode(text);
  let raw="";
  for(let i=0;i<bytes.length;i+=0x8000) raw+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
  return btoa(raw).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/g,"");
}
async function callState(env,op,key,value=null,ttlMs=0){
  const url=relayUrl(env), privateKey=relayPrivateKey(env);
  if(!url||!privateKey) throw new Error("SUPABASE_STATE_NOT_CONFIGURED");
  const q=new URLSearchParams({op:String(op),key:String(key)});
  let statePayload="";
  if(op!=="get"){
    const expiresAt=Date.now()+Number(ttlMs||0);
    statePayload=encodePayload(value);
    q.set("expiresAt",String(expiresAt));
    q.set("valueHash",await sha256Hex(statePayload));
  }
  q.sort();
  const query=q.toString();
  const relayTimestamp=Date.now();
  const relayNonce=randomNonce();
  const signed=await signRelayEnvelope(privateKey,{
    relayTimestamp,relayNonce,method:"POST",path:"/internal/state",apiKey:"",query,
  });
  const r=await fetch(url,{
    method:"POST",
    headers:{
      "content-type":"application/json",
      "cache-control":"no-store",
      "x-region":relayRegion(env),
    },
    body:JSON.stringify({
      method:"POST",path:"/internal/state",query,
      relayTimestamp,relayNonce,relaySignature:signed.relaySignature,
      statePayload,
    }),
    signal:AbortSignal.timeout(10_000),
  });
  const body=await r.json().catch(()=>({}));
  return {httpStatus:r.status,...body};
}

export async function supabaseStateGet(env,key){
  const r=await callState(env,"get",key);
  if(!r.ok) throw new Error(String(r.status||"SUPABASE_STATE_GET_FAILED"));
  return r.found?r.value:null;
}
export async function supabaseStatePut(env,key,value,ttlMs){
  const r=await callState(env,"put",key,value,ttlMs);
  if(!r.ok) throw new Error(String(r.status||"SUPABASE_STATE_PUT_FAILED"));
  return true;
}
export async function supabaseStateClaim(env,key,value,ttlMs){
  const r=await callState(env,"claim",key,value,ttlMs);
  if(r.httpStatus===409||r.status==="STATE_ALREADY_CLAIMED") return false;
  if(!r.ok) throw new Error(String(r.status||"SUPABASE_STATE_CLAIM_FAILED"));
  return r.claimed===true;
}
