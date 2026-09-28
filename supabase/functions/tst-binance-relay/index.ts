import { parseAndValidateCapability, sanitizeResponse } from "./relay-core.mjs";

const BASE = (Deno.env.get("BINANCE_PRIVATE_BASE_URL") || "https://api.binance.com").replace(/\/$/, "");
const WRITES_ENABLED = ["1","true","yes","on"].includes(
  String(Deno.env.get("FINANCIAL_WRITES_ENABLED") || "").toLowerCase()
);

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
    return json(200,{
      ok:true,
      status:"SUPABASE_BINANCE_RELAY_READY",
      financialWritesEnabled:WRITES_ENABLED,
      authMode:"BINANCE_SIGNED_CAPABILITY",
      noBinanceSecretStored:true,
    });
  }

  if(req.method !== "POST"){
    return json(405,{ok:false,status:"METHOD_NOT_ALLOWED"});
  }

  let body:any={};
  try{ body=await req.json(); }
  catch{ return json(400,{ok:false,status:"BAD_JSON"}); }

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
