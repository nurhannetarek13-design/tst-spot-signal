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

# Cloudflare should spend one external subrequest on validator state, not five.
# GitHub Actions materializes the five independent reports into one safe bundle.
validator_re = re.compile(r'async function getFusionValidators\(env,force=false\)\{.*?\n\}\n\nasync function binance\(path\)', re.S)
validator_new = '''async function getFusionValidators(env,force=false){
  const cached=await getState(env,"fusion:validators");
  if(!force&&cached&&Number(cached.fetchedAt||0)>Date.now()-VALIDATOR_CACHE_SECONDS*1000){
    const rows=Object.entries(cached).filter(([k])=>k!=="fetchedAt").map(([,v])=>v);
    const poisoned=rows.length>0&&rows.every(v=>String(v?.error||"").includes("Too many subrequests"));
    if(!poisoned) return cached;
  }

  const bundleUrl="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/validation/fusion/validators-bundle-latest.json";
  try{
    const r=await fetch(bundleUrl,{headers:{Accept:"application/json","User-Agent":"tst-fusion-worker/3.0"},signal:AbortSignal.timeout(8000),cf:{cacheTtl:120,cacheEverything:true}});
    if(!r.ok) throw new Error(String(r.status));
    const bundle=await r.json();
    const reports=bundle?.reports||{};
    const out={};
    for(const name of Object.keys(FUSION_VALIDATORS)){
      const report=reports[name];
      if(!report){
        out[name]={engine:name.toUpperCase(),status:"UNAVAILABLE",pass:false,strategyMatch:false,usable:false,error:"MISSING_FROM_VALIDATOR_BUNDLE"};
        continue;
      }
      const expected=EXPECTED_VALIDATOR_IDS[name]||FUSION_STRATEGY_ID;
      const same=report?.strategyId===expected;
      out[name]={...report,strategyMatch:same,usable:Boolean(same&&report?.generatedAt)};
    }
    out.fetchedAt=Date.now();
    await putState(env,"fusion:validators",out,2*3600);
    return out;
  }catch(error){
    if(cached) return {...cached,bundleRefreshError:String(error?.message||error)};
    const out={};
    for(const name of Object.keys(FUSION_VALIDATORS)) out[name]={engine:name.toUpperCase(),status:"UNAVAILABLE",pass:false,strategyMatch:false,usable:false,error:String(error?.message||error)};
    out.fetchedAt=Date.now();
    return out;
  }
}

async function binance(path)'''
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
