function n(v){const x=Number(v||0);return Number.isFinite(x)?x:0;}
function baseAsset(symbol){const s=String(symbol||"").toUpperCase();return s.endsWith("USDT")?s.slice(0,-4):"";}
export function summarizeBinanceOrder(order,{symbol,commissionQuoteRates={}}={}){
  const fills=Array.isArray(order?.fills)?order.fills:[];
  const grossQty=n(order?.executedQty);
  const quoteQty=n(order?.cummulativeQuoteQty);
  const weightedPrice=grossQty>0?quoteQty/grossQty:0;
  const commissions={};
  for(const f of fills){
    const a=String(f?.commissionAsset||"").toUpperCase();
    if(!a)continue;
    commissions[a]=(commissions[a]||0)+n(f?.commission);
  }
  const base=baseAsset(symbol||order?.symbol);
  const baseCommission=n(commissions[base]);
  const quoteCommission=n(commissions.USDT);
  let convertedOtherCommissionUSDT=0;
  const unresolved={};
  for(const [asset,amount] of Object.entries(commissions)){
    if(asset===base||asset==="USDT")continue;
    const rate=n(commissionQuoteRates[asset]);
    if(rate>0)convertedOtherCommissionUSDT+=amount*rate;
    else unresolved[asset]=amount;
  }
  return {
    symbol:String(symbol||order?.symbol||"").toUpperCase(),
    orderId:String(order?.orderId||""),
    status:String(order?.status||""),
    grossQty,
    netBaseQty:Math.max(0,grossQty-baseCommission),
    quoteQty,
    weightedPrice,
    commissions,
    baseCommission,
    quoteCommission,
    convertedOtherCommissionUSDT,
    unresolvedCommissionAssets:unresolved,
  };
}
export function compareAccounting(bot,binance,{qtyTol=1e-10,quoteTol=1e-8,priceTol=1e-8,feeTol=1e-8}={}){
  const diffs={
    grossQty:n(bot?.grossQty)-n(binance?.grossQty),
    netBaseQty:n(bot?.netBaseQty)-n(binance?.netBaseQty),
    quoteQty:n(bot?.quoteQty)-n(binance?.quoteQty),
    weightedPrice:n(bot?.weightedPrice)-n(binance?.weightedPrice),
    quoteCommission:n(bot?.quoteCommission)-n(binance?.quoteCommission),
    convertedOtherCommissionUSDT:n(bot?.convertedOtherCommissionUSDT)-n(binance?.convertedOtherCommissionUSDT),
    residualQty:n(bot?.residualQty)-n(binance?.residualQty),
    grossPnlUSDT:n(bot?.grossPnlUSDT)-n(binance?.grossPnlUSDT),
    netPnlUSDT:n(bot?.netPnlUSDT)-n(binance?.netPnlUSDT),
  };
  const clean=
    Math.abs(diffs.grossQty)<=qtyTol&&
    Math.abs(diffs.netBaseQty)<=qtyTol&&
    Math.abs(diffs.quoteQty)<=quoteTol&&
    Math.abs(diffs.weightedPrice)<=priceTol&&
    Math.abs(diffs.quoteCommission)<=feeTol&&
    Math.abs(diffs.convertedOtherCommissionUSDT)<=feeTol&&
    Math.abs(diffs.residualQty)<=qtyTol&&
    Math.abs(diffs.grossPnlUSDT)<=quoteTol&&
    Math.abs(diffs.netPnlUSDT)<=quoteTol;
  return {clean,status:clean?"ACCOUNTING_PARITY_OK":"ACCOUNTING_PARITY_MISMATCH",diffs};
}
export function roundTripAccounting({buy,sell,residualQty=0,residualMarkPrice=0}={}){
  const grossPnlUSDT=n(sell?.quoteQty)-n(buy?.quoteQty);
  const feeUSDT=
    n(buy?.quoteCommission)+n(sell?.quoteCommission)+
    n(buy?.convertedOtherCommissionUSDT)+n(sell?.convertedOtherCommissionUSDT);
  const residualValueUSDT=n(residualQty)*n(residualMarkPrice);
  return {
    grossPnlUSDT,
    feeUSDT,
    residualQty:n(residualQty),
    residualValueUSDT,
    netPnlUSDT:grossPnlUSDT-feeUSDT+residualValueUSDT,
  };
}
