const MAX_BUY_QUOTE_USDT = 10;
const MIN_BUY_QUOTE_USDT = 5;

function exactKeys(actual, expected) {
  const a=[...actual].sort(), e=[...expected].sort();
  return a.length===e.length && a.every((x,i)=>x===e[i]);
}
function safeSymbol(v) {
  return /^[A-Z0-9]{1,20}USDT$/.test(String(v||""));
}
function safeClientId(v,prefix) {
  const s=String(v||"");
  return s.startsWith(prefix) && /^[A-Za-z0-9_-]{5,36}$/.test(s);
}
function finitePositive(v) {
  const n=Number(v);
  return Number.isFinite(n) && n>0 ? n : null;
}
function validSignatureShape(v) {
  const s=String(v||"");
  return /^[a-f0-9]{64}$/i.test(s) || /^[A-Za-z0-9+/=]{64,1024}$/.test(s);
}

export function validateSignedOperation(method,path,query,now=Date.now()) {
  method=String(method||"").toUpperCase();
  path=String(path||"");
  if (typeof query!=="string" || query.length<70 || query.length>4096) {
    return {ok:false,status:"BAD_SIGNED_QUERY"};
  }

  const q=new URLSearchParams(query);
  const entries=[...q.entries()];
  const keys=entries.map(([k])=>k);
  if (new Set(keys).size!==keys.length) return {ok:false,status:"DUPLICATE_QUERY_KEY"};

  const timestamp=Number(q.get("timestamp"));
  const recvWindow=Number(q.get("recvWindow"));
  const signature=String(q.get("signature")||"");
  if (!Number.isFinite(timestamp) || Math.abs(now-timestamp)>60_000) return {ok:false,status:"STALE_BINANCE_QUERY"};
  if (!Number.isFinite(recvWindow) || recvWindow<1 || recvWindow>60_000) return {ok:false,status:"BAD_RECV_WINDOW"};
  if (!validSignatureShape(signature)) return {ok:false,status:"BAD_BINANCE_SIGNATURE_SHAPE"};

  const common=["recvWindow","timestamp","signature"];
  if (method==="GET" && path==="/api/v3/account") {
    if (!exactKeys(keys,common)) return {ok:false,status:"ACCOUNT_QUERY_SHAPE_NOT_ALLOWED"};
    return {ok:true,kind:"ACCOUNT_READ"};
  }

  if (method==="POST" && path==="/api/v3/order") {
    const side=String(q.get("side")||"");
    const type=String(q.get("type")||"");
    if (!safeSymbol(q.get("symbol")) || type!=="MARKET" || String(q.get("newOrderRespType")||"")!=="FULL") {
      return {ok:false,status:"ORDER_SHAPE_NOT_ALLOWED"};
    }

    if (side==="BUY") {
      const expected=[...common,"symbol","side","type","quoteOrderQty","newOrderRespType","newClientOrderId"];
      if (!exactKeys(keys,expected) || !safeClientId(q.get("newClientOrderId"),"TSTU")) {
        return {ok:false,status:"BUY_QUERY_NOT_ALLOWED"};
      }
      const quote=finitePositive(q.get("quoteOrderQty"));
      if (quote===null || quote<MIN_BUY_QUOTE_USDT || quote>MAX_BUY_QUOTE_USDT) {
        return {ok:false,status:"BUY_QUOTE_OUTSIDE_LIMIT"};
      }
      return {ok:true,kind:"SPOT_MARKET_BUY"};
    }

    if (side==="SELL") {
      const expected=[...common,"symbol","side","type","quantity","newOrderRespType","newClientOrderId"];
      if (!exactKeys(keys,expected) || !safeClientId(q.get("newClientOrderId"),"TSTE")) {
        return {ok:false,status:"EMERGENCY_SELL_QUERY_NOT_ALLOWED"};
      }
      const quantity=finitePositive(q.get("quantity"));
      if (quantity===null || quantity>1e15) return {ok:false,status:"EMERGENCY_SELL_QTY_INVALID"};
      return {ok:true,kind:"SPOT_EMERGENCY_MARKET_SELL"};
    }
    return {ok:false,status:"ORDER_SIDE_NOT_ALLOWED"};
  }

  if (method==="POST" && path==="/api/v3/orderList/oco") {
    const expected=[
      ...common,"symbol","side","quantity","aboveType","abovePrice","belowType",
      "belowStopPrice","belowPrice","belowTimeInForce","listClientOrderId",
    ];
    if (!exactKeys(keys,expected) ||
        !safeSymbol(q.get("symbol")) ||
        String(q.get("side"))!=="SELL" ||
        String(q.get("aboveType"))!=="LIMIT_MAKER" ||
        String(q.get("belowType"))!=="STOP_LOSS_LIMIT" ||
        String(q.get("belowTimeInForce"))!=="GTC" ||
        !safeClientId(q.get("listClientOrderId"),"TSTO")) {
      return {ok:false,status:"OCO_QUERY_NOT_ALLOWED"};
    }
    const quantity=finitePositive(q.get("quantity"));
    const above=finitePositive(q.get("abovePrice"));
    const stop=finitePositive(q.get("belowStopPrice"));
    const below=finitePositive(q.get("belowPrice"));
    if (quantity===null || above===null || stop===null || below===null || !(above>stop && stop>=below)) {
      return {ok:false,status:"OCO_GEOMETRY_INVALID"};
    }
    return {ok:true,kind:"SPOT_OCO_SELL"};
  }

  return {ok:false,status:"OPERATION_NOT_ALLOWED"};
}

export const policyConstants = {
  MAX_BUY_QUOTE_USDT,
  MIN_BUY_QUOTE_USDT,
};
