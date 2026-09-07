from pathlib import Path
import re

# Railway proxy is the primary public Binance data path; Vercel/direct are fallbacks.
p = Path("src/edge-worker.js")
s = p.read_text()

canonical = '  "https://api.binance.com",\n'
if canonical not in s:
    marker = 'const API_BASES = [\n'
    if marker not in s:
        raise SystemExit("API_BASES marker missing; refusing unsafe patch")
    s = s.replace(marker, marker + canonical, 1)

# Do not let a temporary Cloudflare subrequest-budget failure poison the validator
# cache for 30 minutes. Retry bad cached snapshots and fetch validators sequentially.
validator_re = re.compile(r'async function getFusionValidators\(env,force=false\)\{.*?\n\}\n\nasync function binance\(path\)', re.S)
validator_new = '''async function getFusionValidators(env,force=false){
  const cached=await getState(env,"fusion:validators");
  const cachedRows=cached?Object.entries(cached).filter(([k])=>k!=="fetchedAt").map(([,v])=>v):[];
  const poisoned=cachedRows.length>0&&cachedRows.every(v=>String(v?.error||"").includes("Too many subrequests"));
  if(!force&&!poisoned&&cached&&Number(cached.fetchedAt||0)>Date.now()-VALIDATOR_CACHE_SECONDS*1000) return cached;

  const entries=[];
  for(const [name,url] of Object.entries(FUSION_VALIDATORS)){
    try{
      const r=await fetch(url,{headers:{Accept:"application/json","User-Agent":"tst-fusion-worker/2.0"},signal:AbortSignal.timeout(8000),cf:{cacheTtl:300,cacheEverything:true}});
      if(!r.ok) throw new Error(String(r.status));
      const report=await r.json();
      const expected=EXPECTED_VALIDATOR_IDS[name]||FUSION_STRATEGY_ID;
      const same=report?.strategyId===expected;
      entries.push([name,{...report,strategyMatch:same,usable:Boolean(same&&report?.generatedAt)}]);
    }catch(error){
      entries.push([name,{engine:name.toUpperCase(),status:"UNAVAILABLE",pass:false,strategyMatch:false,usable:false,error:String(error?.message||error)}]);
    }
  }
  const out=Object.fromEntries(entries); out.fetchedAt=Date.now();
  const rows=entries.map(([,v])=>v);
  const allBudgetErrors=rows.length>0&&rows.every(v=>String(v?.error||"").includes("Too many subrequests"));
  if(!allBudgetErrors) await putState(env,"fusion:validators",out,2*3600);
  return out;
}

async function binance(path)'''
if 'const poisoned=cachedRows.length>0' not in s:
    s2,n=validator_re.subn(validator_new,s,count=1)
    if n!=1:
        raise SystemExit("getFusionValidators implementation changed; refusing unsafe patch")
    s=s2

old = 'async function binance(path){let last;for(const base of API_BASES){try{const r=await fetch(base+path,{headers:{Accept:"application/json","User-Agent":"tst-edge-worker/2.0"},signal:AbortSignal.timeout(12000)});if(!r.ok)throw new Error(`${r.status}`);return await r.json();}catch(e){last=e;}}throw last||new Error("Binance unavailable");}'
new = '''async function binance(path){
  let last;
  const proxies=[
    `https://liquidation-collector-v2-production.up.railway.app/api/binance-public?path=${encodeURIComponent(path)}`,
    `https://tst-spot-signal.vercel.app/api/binance-public?path=${encodeURIComponent(path)}`,
  ];
  for(const proxy of proxies){
    try{
      const r=await fetch(proxy,{headers:{Accept:"application/json","User-Agent":"tst-edge-worker/4.0"},signal:AbortSignal.timeout(15000)});
      if(r.ok) return await r.json();
      last=new Error(`Binance proxy ${r.status}`);
    }catch(e){ last=e; }
  }
  for(const base of API_BASES){
    try{
      const r=await fetch(base+path,{headers:{Accept:"application/json","User-Agent":"tst-edge-worker/4.0"},signal:AbortSignal.timeout(12000)});
      if(!r.ok)throw new Error(`${r.status}`);
      return await r.json();
    }catch(e){last=e;}
  }
  throw last||new Error("Binance unavailable");
}'''
if new not in s:
    if old not in s:
        raise SystemExit("binance() implementation changed; refusing unsafe patch")
    s = s.replace(old, new, 1)

p.write_text(s)
