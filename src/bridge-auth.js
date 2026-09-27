const SECRET_KEY="bridge:secret:v2";
const TTL=600;
function stub(env){const id=env.STATE_COORDINATOR.idFromName("global");return env.STATE_COORDINATOR.get(id);}
async function get(env,k){const r=await stub(env).fetch(`https://state/get?key=${encodeURIComponent(k)}`);return r.ok?await r.json():null;}
async function put(env,k,v,ttl=31536000){await stub(env).fetch(`https://state/put?key=${encodeURIComponent(k)}`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify({value:v,expiresAt:Date.now()+ttl*1000})});}
async function claim(env,k,v,ttl=TTL){const r=await stub(env).fetch(`https://state/claim?key=${encodeURIComponent(k)}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({value:v,expiresAt:Date.now()+ttl*1000})});return r.ok;}
function enc(bytes){let s="";for(const b of bytes)s+=String.fromCharCode(b);return btoa(s).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/g,"");}
async function secret(env){let s=await get(env,SECRET_KEY);if(typeof s==="string"&&s.length>30)return s;const b=new Uint8Array(32);crypto.getRandomValues(b);s=enc(b);await put(env,SECRET_KEY,s);return s;}
async function mac(sec,msg){const k=await crypto.subtle.importKey("raw",new TextEncoder().encode(sec),{name:"HMAC",hash:"SHA-256"},false,["sign"]);const sig=await crypto.subtle.sign("HMAC",k,new TextEncoder().encode(msg));return [...new Uint8Array(sig)].map(x=>x.toString(16).padStart(2,"0")).join("");}
function safeEq(a,b){a=String(a||"").toLowerCase();b=String(b||"").toLowerCase();if(a.length!==64||b.length!==64)return false;let d=0;for(let i=0;i<64;i++)d|=a.charCodeAt(i)^b.charCodeAt(i);return d===0;}
function logical(packed){const p=String(packed||"").split(".");if(p.length!==3)return null;return {id:p[0],nonce:p[1],sig:p[2]};}
function canonical(b,id){return JSON.stringify({signal_id:String(id||""),action:String(b.action||""),symbol:String(b.symbol||"").toUpperCase(),quote_amount_usdt:Number(b.quote_amount_usdt||0),take_profit_price:Number(b.take_profit_price||0),stop_loss_price:Number(b.stop_loss_price||0),confirmed:b.confirmed===true,dry_run:b.dry_run===true,timestamp:Number(b.timestamp||0)});}
export async function signBridgeEnvelope(env,input){
 const ts=Math.floor(Date.now()/1000),nbytes=new Uint8Array(18);crypto.getRandomValues(nbytes);const nonce=enc(nbytes);
 const id=String(input.signal_id||"sig").replace(/[^A-Za-z0-9_-]/g,"").slice(0,24)||"sig";
 const body={...input,signal_id:id,timestamp:ts};const can=canonical(body,id);const s=await secret(env);const sig=await mac(s,`${ts}.${nonce}.${can}`);
 await put(env,`bridge:issued:${nonce}`,{id,can,at:Date.now()},TTL);
 return {...body,signal_id:`${id}.${nonce}.${sig}`};
}
export async function verifyBridgeEnvelope(env,body){
 const p=logical(body?.signal_id);if(!p)return {ok:false,status:"BAD_ENVELOPE"};
 const ts=Number(body?.timestamp||0);if(!Number.isFinite(ts)||Math.abs(Date.now()-ts*1000)>90000)return {ok:false,status:"STALE"};
 const issued=await get(env,`bridge:issued:${p.nonce}`);if(!issued||issued.id!==p.id)return {ok:false,status:"NONCE_NOT_ISSUED"};
 const can=canonical(body,p.id);if(can!==issued.can)return {ok:false,status:"PAYLOAD_MISMATCH"};
 const exp=await mac(await secret(env),`${ts}.${p.nonce}.${can}`);if(!safeEq(p.sig,exp))return {ok:false,status:"BAD_HMAC"};
 if(!await claim(env,`bridge:used:${p.nonce}`,{id:p.id,at:Date.now()},TTL))return {ok:false,status:"REPLAY_BLOCKED"};
 return {ok:true,status:"BRIDGE_AUTH_OK",logicalSignalId:p.id};
}
export async function rotateBridgeSecret(env){const b=new Uint8Array(32);crypto.getRandomValues(b);await put(env,SECRET_KEY,enc(b));return {ok:true,status:"BRIDGE_SECRET_ROTATED",version:"v2"};}
