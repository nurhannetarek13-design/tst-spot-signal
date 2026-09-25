function finitePositive(value,name){
  const n=Number(value);
  if(!Number.isFinite(n)||n<=0) throw new Error(name+"_INVALID");
  return n;
}

export function v3PaperPositionToExecutionIntent(symbol, position={}){
  const sym=String(symbol||"").toUpperCase();
  if(!/^[A-Z0-9]{5,20}$/.test(sym)||!sym.endsWith("USDT")) throw new Error("V3_INVALID_SPOT_SYMBOL");
  if(String(position.engine||"")!=="INDICATOR_ONLY_V3_EARLY_MOMENTUM") throw new Error("V3_ENGINE_MISMATCH");
  const signalId=String(position.signal_id||"").trim();
  if(!signalId) throw new Error("V3_SIGNAL_ID_REQUIRED");

  const plan=position.execution_plan||{};
  const style=String(plan.style||"").toUpperCase();
  if(!["MARKET","AGGRESSIVE_LIMIT"].includes(style)) throw new Error("V3_EXECUTION_STYLE_INVALID");

  const entryPrice=finitePositive(position.entry,"entry");
  const stopPrice=finitePositive(position.stop,"stop");
  const takeProfitPrice=finitePositive(position.target,"target");
  const quoteAmountUsdt=finitePositive(position.cost,"cost");
  if(stopPrice>=entryPrice) throw new Error("V3_STOP_NOT_BELOW_ENTRY");
  if(takeProfitPrice<=entryPrice) throw new Error("V3_TARGET_NOT_ABOVE_ENTRY");

  return {
    schema:"V3_SPOT_EXECUTION_INTENT_V1",
    authorization:"SHADOW_ONLY",
    liveApproved:false,
    signalId,
    symbol:sym,
    side:"BUY",
    quoteAmountUsdt,
    entry:{
      style,
      referencePrice:finitePositive(plan.reference_price??entryPrice,"reference_price"),
      limitPrice:style==="AGGRESSIVE_LIMIT"?finitePositive(plan.limit_price,"limit_price"):null,
      cancelAfterMs:style==="AGGRESSIVE_LIMIT"?Number(plan.cancel_after_ms||0):null,
      maxCancelReplace:style==="AGGRESSIVE_LIMIT"?Number(plan.max_cancel_replace||0):0,
    },
    protection:{
      required:true,
      stopPrice,
      takeProfitPrice,
    },
    diagnostics:{
      modeledFillPrice:entryPrice,
      estimatedSlippageBps:Number.isFinite(Number(plan.estimated_slippage_bps))?Number(plan.estimated_slippage_bps):null,
      momentumScore:Number.isFinite(Number(position.momentum_score))?Number(position.momentum_score):null,
      marketRegime:position.market_regime??null,
    }
  };
}

export function executionIntentIdempotencyKey(intent){
  if(!intent||intent.schema!=="V3_SPOT_EXECUTION_INTENT_V1") throw new Error("V3_INTENT_SCHEMA_INVALID");
  if(!intent.signalId) throw new Error("V3_SIGNAL_ID_REQUIRED");
  return "v3:"+intent.symbol+":"+intent.signalId;
}

export function assertV3IntentShadowOnly(intent){
  if(intent?.liveApproved===true||intent?.authorization!=="SHADOW_ONLY") throw new Error("V3_LIVE_INTENT_FORBIDDEN");
  return true;
}
