const STABLE_BASES = new Set(['USDC','FDUSD','TUSD','USDP','DAI','BUSD','EUR','AEUR','TRY','BRL','GBP','AUD','USD1','RLUSD','USDE']);
const EXCLUDED_BASES = new Set(['BTC','ETH','BNB','SOL','XRP','ADA','DOGE','TRX','XAUT','PAXG']);

export const STRATEGY_ID = 'SMALL_CAP_INTRADAY_MOMENTUM_V1';
export const LIVE_APPROVED = false;

export const DEFAULTS = Object.freeze({
  minQuoteVolume24h: 5_000_000,
  maxQuoteVolume24h: 150_000_000,
  maxSpreadPct: 0.15,
  emaFast: 20,
  emaTrend: 50,
  atrPeriod: 14,
  minTakerBuyRatio: 0.55,
  minRelativeVolume: 1.30,
  minScore: 75,
  stopAtrMult: 0.90,
  targetAtrMult: 1.40,
  minTargetPct: 1.00,
  maxHoldBars: 8,
  maxRiskUSDT: 0.10,
  maxPositionUSDT: 5.50,
});

export function supportedBase(base){
  if (!base || STABLE_BASES.has(base) || EXCLUDED_BASES.has(base)) return false;
  if (['UP','DOWN','BULL','BEAR'].some(s=>base.endsWith(s))) return false;
  return true;
}

export function ema(values,n){
  if(!values?.length) return [];
  const k=2/(n+1); let v=values[0];
  return values.map(x=>(v=x*k+v*(1-k)));
}

export function atr(candles,n=14){
  if(!candles?.length) return [];
  const tr=candles.map((x,i)=>i===0?x.h-x.l:Math.max(x.h-x.l,Math.abs(x.h-candles[i-1].c),Math.abs(x.l-candles[i-1].c)));
  let v=tr[0];
  return tr.map((x,i)=>(v=i<n?tr.slice(0,i+1).reduce((a,b)=>a+b,0)/(i+1):(v*(n-1)+x)/n));
}

export function scoreCandidate({symbol,baseAsset,c15,c1h,btc15,quoteVolume24h,bid,ask,takerBuyRatio,relativeVolume},cfg=DEFAULTS){
  const fail=(reason,extra={})=>({ok:false,symbol,reason,strategy:STRATEGY_ID,liveApproved:false,...extra});
  if(!symbol?.endsWith('USDT')||!supportedBase(baseAsset)) return fail('UNSUPPORTED_SYMBOL');
  if(!c15||c15.length<80||!c1h||c1h.length<80||!btc15||btc15.length<10) return fail('INSUFFICIENT_HISTORY');
  if(!(quoteVolume24h>=cfg.minQuoteVolume24h&&quoteVolume24h<=cfg.maxQuoteVolume24h)) return fail('OUTSIDE_SMALL_CAP_LIQUIDITY_BAND');
  if(!(bid>0&&ask>bid)) return fail('BAD_BOOK');
  const spreadPct=((ask-bid)/ask)*100;
  if(spreadPct>cfg.maxSpreadPct) return fail('SPREAD_TOO_WIDE',{spreadPct});

  const x15=c15.map(x=>x.c), x1=c1h.map(x=>x.c), b=btc15.map(x=>x.c);
  const i=x15.length-1, j=x1.length-1, bi=b.length-1;
  const e20=ema(x15,cfg.emaFast), e50=ema(x15,cfg.emaTrend), e1h=ema(x1,cfg.emaTrend);
  const mom1h=x15[i]/x15[i-4]-1;
  const mom4h=x15[i]/x15[i-16]-1;
  const btc1h=b[bi]/b[bi-4]-1;
  const recentHigh=Math.max(...c15.slice(-13,-1).map(x=>x.h));
  const breakout=x15[i]>recentHigh;
  const trend15=x15[i]>e20[i]&&e20[i]>e50[i];
  const trend1h=x1[j]>e1h[j];
  const btcCrash=btc1h<=-0.015;
  if(btcCrash) return fail('BTC_FAST_CRASH_FILTER',{btc1h});
  if(!(mom1h>0&&mom4h>0&&trend15&&trend1h)) return fail('LOCAL_MOMENTUM_NOT_CONFIRMED',{mom1h,mom4h});

  const a=atr(c15,cfg.atrPeriod); const atrNow=a[i]; const atrPct=atrNow/x15[i];
  const orderFlowOk=Number.isFinite(takerBuyRatio)&&takerBuyRatio>=cfg.minTakerBuyRatio;
  const volumeOk=Number.isFinite(relativeVolume)&&relativeVolume>=cfg.minRelativeVolume;
  let score=0;
  score+=trend15?20:0; score+=trend1h?15:0;
  score+=Math.min(20,Math.max(0,mom1h*800));
  score+=Math.min(15,Math.max(0,mom4h*250));
  score+=breakout?10:0; score+=orderFlowOk?10:0; score+=volumeOk?10:0;
  score+=spreadPct<=cfg.maxSpreadPct/2?5:0;
  score=Math.round(Math.min(100,score));

  const entry=ask;
  const stopDistance=Math.max(cfg.stopAtrMult*atrNow,entry*0.0045);
  const targetDistance=Math.max(cfg.targetAtrMult*atrNow,entry*(cfg.minTargetPct/100));
  const stop=entry-stopDistance, target=entry+targetDistance;
  const qtyByRisk=cfg.maxRiskUSDT/stopDistance;
  const qtyByNotional=cfg.maxPositionUSDT/entry;
  const qty=Math.max(0,Math.min(qtyByRisk,qtyByNotional));
  const ok=score>=cfg.minScore&&orderFlowOk&&volumeOk;
  return {ok,symbol,strategy:STRATEGY_ID,liveApproved:false,score,entry,stop,target,qty,notional:qty*entry,maxHoldBars:cfg.maxHoldBars,reason:ok?'PAPER_CANDIDATE':'CONFIRMATION_INCOMPLETE',metrics:{spreadPct,mom1h,mom4h,btc1h,atrPct,takerBuyRatio,relativeVolume,breakout,trend15,trend1h}};
}
