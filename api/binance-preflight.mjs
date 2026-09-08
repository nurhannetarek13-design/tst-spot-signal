import crypto from 'node:crypto';

const KEY_NAMES = ['BINANCE_API_KEY','BINANCE_KEY','BINANCE_KEY_API','BINANCE_SPOT_API_KEY'];
const SECRET_NAMES = ['BINANCE_API_SECRET','BINANCE_SECRET_KEY','BINANCE_SECRET','BINANCE_API_SECRET_KEY','BINANCE_SPOT_API_SECRET'];
const BASES = ['https://api.binance.com','https://api-gcp.binance.com','https://api1.binance.com','https://api2.binance.com','https://api3.binance.com','https://api4.binance.com'];

async function tryPair(keyName, secretName) {
  const key = (process.env[keyName] || '').trim();
  const secret = (process.env[secretName] || '').trim();
  if (!key || !secret) return {keyName, secretName, present:false};
  const qs = new URLSearchParams([['recvWindow','5000'],['timestamp',String(Date.now())]]).toString();
  const signature = crypto.createHmac('sha256', secret).update(qs).digest('hex');
  for (const base of BASES) {
    try {
      const r = await fetch(`${base}/api/v3/account?${qs}&signature=${signature}`, {headers:{'X-MBX-APIKEY':key,'User-Agent':'tst-binance-preflight/1.0'},signal:AbortSignal.timeout(10000)});
      const text = await r.text();
      let data={}; try{data=JSON.parse(text)}catch{}
      if (r.ok && !data?.code) return {keyName,secretName,present:true,ok:true,canTrade:Boolean(data.canTrade),accountType:data.accountType||null};
      if (r.status === 451) return {keyName,secretName,present:true,ok:false,status:451,error:'geo-blocked'};
      if (data?.code === -1022) continue;
      return {keyName,secretName,present:true,ok:false,status:r.status,code:data?.code||null,error:String(data?.msg||'request failed').slice(0,120)};
    } catch (e) {}
  }
  return {keyName,secretName,present:true,ok:false,code:-1022,error:'signature mismatch on all endpoints'};
}

export default async function handler(req,res){
  const presentKeys=KEY_NAMES.filter(n=>(process.env[n]||'').trim());
  const presentSecrets=SECRET_NAMES.filter(n=>(process.env[n]||'').trim());
  const results=[];
  for(const k of presentKeys){for(const s of presentSecrets){results.push(await tryPair(k,s));}}
  const winner=results.find(x=>x.ok)||null;
  res.status(winner?200:503).setHeader('Cache-Control','no-store').json({ok:Boolean(winner),presentKeys,presentSecrets,winner:winner?{keyName:winner.keyName,secretName:winner.secretName,canTrade:winner.canTrade,accountType:winner.accountType}:null,results:results.map(x=>({keyName:x.keyName,secretName:x.secretName,present:x.present,ok:Boolean(x.ok),status:x.status||null,code:x.code||null,error:x.error||null}))});
}
