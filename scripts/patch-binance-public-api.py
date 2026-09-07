from pathlib import Path

# Railway proxy is the primary public Binance data path; Vercel/direct are fallbacks.
p = Path("src/edge-worker.js")
s = p.read_text()

canonical = '  "https://api.binance.com",\n'
if canonical not in s:
    marker = 'const API_BASES = [\n'
    if marker not in s:
        raise SystemExit("API_BASES marker missing; refusing unsafe patch")
    s = s.replace(marker, marker + canonical, 1)

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
