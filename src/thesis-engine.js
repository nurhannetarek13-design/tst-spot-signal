function median(values){
  if(!Array.isArray(values)||!values.length) return 0;
  const a=[...values].filter(Number.isFinite).sort((x,y)=>x-y);
  if(!a.length) return 0;
  const h=Math.floor(a.length/2);
  return a.length%2?a[h]:(a[h-1]+a[h])/2;
}

function ema(values,period){
  if(!Array.isArray(values)||!values.length) return 0;
  const alpha=2/(period+1);
  let out=Number(values[0]||0);
  for(let i=1;i<values.length;i++) out=Number(values[i]||0)*alpha+out*(1-alpha);
  return out;
}

function recentSwingHigh(candles,endExclusive,lookback=20){
  const end=Math.max(1,Math.min(endExclusive,candles.length));
  const start=Math.max(0,end-lookback);
  return Math.max(...candles.slice(start,end).map(x=>Number(x.high||0)));
}

function recentSwingLow(candles,endExclusive,lookback=20){
  const end=Math.max(1,Math.min(endExclusive,candles.length));
  const start=Math.max(0,end-lookback);
  return Math.min(...candles.slice(start,end).map(x=>Number(x.low||Infinity)));
}

function bullishFvg(candles,startIndex){
  for(let i=Math.max(2,startIndex);i<candles.length;i++){
    const left=candles[i-2],right=candles[i];
    if(Number(right.low)>Number(left.high)){
      return {
        createdIndex:i,
        low:Number(left.high),
        high:Number(right.low),
        midpoint:(Number(left.high)+Number(right.low))/2,
      };
    }
  }
  return null;
}

function structureTargets(candles,entry,risk){
  const highs=[];
  for(let i=2;i<candles.length-2;i++){
    const h=Number(candles[i].high||0);
    if(
      h>Number(candles[i-1].high||0)&&
      h>=Number(candles[i-2].high||0)&&
      h>Number(candles[i+1].high||0)&&
      h>=Number(candles[i+2].high||0)&&
      h>entry+risk*1.1
    ) highs.push(h);
  }
  highs.sort((a,b)=>a-b);
  const unique=highs.filter((x,i)=>i===0||Math.abs(x-highs[i-1])>entry*0.0015);
  const target1=unique[0]||entry+risk*1.5;
  const target2=unique.find(x=>x>target1+risk*0.45)||entry+risk*2.5;
  return {
    target1:Math.max(target1,entry+risk*1.2),
    target2:Math.max(target2,entry+risk*1.8),
    source:unique.length?"STRUCTURE_LIQUIDITY":"R_MULTIPLE_FALLBACK",
  };
}

function detectLiquiditySweepFvg(c){
  if(!Array.isArray(c)||c.length<40) return null;
  const start=Math.max(22,c.length-7);
  for(let i=start;i<c.length;i++){
    const bar=c[i];
    const priorLow=recentSwingLow(c,i,20);
    const swept=Number(bar.low)<priorLow*0.998;
    const reclaimed=Number(bar.close)>priorLow&&Number(bar.close)>Number(bar.open);
    if(!(swept&&reclaimed)) continue;
    const gap=bullishFvg(c,i+1);
    if(!gap) continue;
    const last=c.at(-1);
    const retested=Number(last.low)<=gap.high*1.003&&Number(last.close)>=gap.midpoint;
    if(!retested) continue;
    const stop=Number(bar.low)*0.996;
    return {
      strategy:"LIQUIDITY_SWEEP_FVG",
      setup:"SELL_SIDE_SWEEP_RECLAIM_FVG_RETEST",
      signalBar:Number(bar.openTime||0),
      stop,
      setupQuality:88,
      patternEvidence:[
        "sell-side liquidity swept below 20-bar low",
        "sweep candle reclaimed prior low",
        "bullish fair value gap formed after reclaim",
        "price retested and held the FVG midpoint",
      ],
      invalidationReason:"close/acceptance below sweep low invalidates the reclaim thesis",
    };
  }
  return null;
}

function detectBreakoutRetestContinuation(c){
  if(!Array.isArray(c)||c.length<40) return null;
  const last=c.at(-1);
  for(let i=Math.max(22,c.length-7);i<c.length-1;i++){
    const breakout=c[i];
    const resistance=recentSwingHigh(c,i,20);
    if(!(Number(breakout.close)>resistance*1.001&&Number(breakout.close)>Number(breakout.open))) continue;
    const after=c.slice(i+1);
    const held=after.every(x=>Number(x.close)>=resistance*0.996);
    const retest=after.some(x=>Number(x.low)<=resistance*1.004&&Number(x.low)>=resistance*0.992);
    const continuation=Number(last.close)>=resistance&&Number(last.close)>=Number(last.open);
    if(!(held&&retest&&continuation)) continue;
    const swingLow=Math.min(...after.map(x=>Number(x.low)),Number(breakout.low));
    return {
      strategy:"BREAKOUT_RETEST_CONTINUATION",
      setup:"STRUCTURE_BREAK_RETEST_HOLD",
      signalBar:Number(breakout.openTime||0),
      stop:swingLow*0.997,
      setupQuality:84,
      patternEvidence:[
        "20-bar resistance was broken on a closing basis",
        "post-break candles held above broken structure",
        "retest returned to former resistance",
        "continuation candle closed back above the level",
      ],
      invalidationReason:"acceptance back below broken resistance invalidates continuation",
    };
  }
  return null;
}

function detectStopHuntStructureShift(c){
  if(!Array.isArray(c)||c.length<45) return null;
  for(let i=Math.max(24,c.length-8);i<c.length-2;i++){
    const sweep=c[i];
    const priorLow=recentSwingLow(c,i,20);
    if(!(Number(sweep.low)<priorLow*0.997&&Number(sweep.close)>priorLow)) continue;
    const preShiftHigh=recentSwingHigh(c,i+1,6);
    const after=c.slice(i+1);
    const shift=after.find(x=>Number(x.close)>preShiftHigh*1.001);
    if(!shift) continue;
    const last=c.at(-1);
    if(Number(last.close)<priorLow) continue;
    return {
      strategy:"STOP_HUNT_STRUCTURE_SHIFT",
      setup:"LIQUIDITY_GRAB_MARKET_STRUCTURE_SHIFT",
      signalBar:Number(shift.openTime||0),
      stop:Number(sweep.low)*0.996,
      setupQuality:90,
      patternEvidence:[
        "sell-side stop hunt printed below prior swing low",
        "price reclaimed the swept liquidity",
        "subsequent candle closed above local swing high",
        "market structure shifted bullish after the stop hunt",
      ],
      invalidationReason:"a return below the stop-hunt low invalidates the bullish structure shift",
    };
  }
  return null;
}

function detectCompressionExpansion(c){
  if(!Array.isArray(c)||c.length<50) return null;
  const last=c.at(-1);
  const compression=c.slice(-9,-1);
  const baseline=c.slice(-38,-9);
  const compRange=median(compression.map(x=>Number(x.high)-Number(x.low)));
  const baseRange=median(baseline.map(x=>Number(x.high)-Number(x.low)));
  if(!(baseRange>0&&compRange/baseRange<=0.68)) return null;
  const ceiling=Math.max(...compression.map(x=>Number(x.high)));
  const floor=Math.min(...compression.map(x=>Number(x.low)));
  const expanded=Number(last.close)>ceiling*1.001&&Number(last.close)>Number(last.open);
  if(!expanded) return null;
  return {
    strategy:"COMPRESSION_EXPANSION",
    setup:"RANGE_COMPRESSION_STRUCTURE_EXPANSION",
    signalBar:Number(last.openTime||0),
    stop:floor*0.997,
    setupQuality:80,
    patternEvidence:[
      "recent candle ranges compressed materially versus baseline",
      "price closed above compression ceiling",
      "expansion occurred from a defined structural range",
    ],
    invalidationReason:"failure back inside/below the compression floor invalidates expansion",
  };
}

export function detectThesisSetup(candles){
  const detectors=[
    detectLiquiditySweepFvg,
    detectStopHuntStructureShift,
    detectBreakoutRetestContinuation,
    detectCompressionExpansion,
  ];
  const candidates=detectors.map(fn=>fn(candles)).filter(Boolean);
  if(!candidates.length) return null;
  return candidates.sort((a,b)=>Number(b.setupQuality||0)-Number(a.setupQuality||0))[0];
}

function tfState(candles){
  if(!Array.isArray(candles)||candles.length<55) return "UNKNOWN";
  const closes=candles.map(x=>Number(x.close||0));
  const e20=ema(closes,20),e50=ema(closes,50),last=closes.at(-1);
  if(last>e20&&e20>e50) return "BULLISH";
  if(last<e20&&e20<e50) return "BEARISH";
  return "NEUTRAL";
}

export function higherTimeframeContext(c15,h1,h4){
  const m15=tfState(c15),oneHour=tfState(h1),fourHour=tfState(h4);
  const bullishVotes=[m15,oneHour,fourHour].filter(x=>x==="BULLISH").length;
  const bearishVotes=[m15,oneHour,fourHour].filter(x=>x==="BEARISH").length;
  return {
    m15,
    h1:oneHour,
    h4:fourHour,
    bullishVotes,
    bearishVotes,
    bullishAlignment:oneHour==="BULLISH"&&fourHour!=="BEARISH",
    hostileAlignment:oneHour==="BEARISH"&&fourHour==="BEARISH",
  };
}

export function calibrationBySetup(ledger){
  const out={};
  for(const trade of Array.isArray(ledger)?ledger:[]){
    const key=String(trade.setup||trade.strategy||"UNKNOWN");
    if(!out[key]) out[key]={trades:0,wins:0,netPnl:0};
    out[key].trades+=1;
    const pnl=Number(trade.pnl||0);
    if(pnl>0) out[key].wins+=1;
    out[key].netPnl+=pnl;
  }
  for(const row of Object.values(out)){
    row.winRate=row.trades?row.wins/row.trades:0;
    row.expectancy=row.trades?row.netPnl/row.trades:0;
    const priorStrength=20;
    row.bayesianWinRate=(row.wins+priorStrength*0.5)/(row.trades+priorStrength);
    row.status=row.trades>=30?"CALIBRATED":row.trades>=10?"EARLY_SAMPLE":"LOW_SAMPLE";
  }
  return out;
}

export function buildThesisDecision({
  setup,
  candles,
  entry,
  htf,
  marketRegime,
  confirmations,
  calibration,
  minNetRR=1.8,
}){
  if(!setup) return null;
  const stop=Number(setup.stop||0);
  const risk=entry-stop;
  if(!(entry>0&&stop>0&&risk>0)) return null;
  const targets=structureTargets(candles,entry,risk);
  const grossRR1=(targets.target1-entry)/risk;
  const grossRR2=(targets.target2-entry)/risk;
  const required={
    marketContext:marketRegime!=="RISK_OFF"||setup.strategy==="STOP_HUNT_STRUCTURE_SHIFT"||setup.strategy==="LIQUIDITY_SWEEP_FVG",
    htfContext:setup.strategy==="STOP_HUNT_STRUCTURE_SHIFT"||setup.strategy==="LIQUIDITY_SWEEP_FVG"
      ? !htf.hostileAlignment
      : htf.bullishAlignment,
    liquidity:Boolean(confirmations.liquidity),
    flow:Boolean(confirmations.flow),
    microstructure:Boolean(confirmations.microstructure),
    executionQuality:Boolean(confirmations.executionQuality),
    rr:grossRR2>=minNetRR,
  };
  const passed=Object.values(required).every(Boolean);
  const optional=[
    confirmations.relVolStrong,
    confirmations.takerStrong,
    confirmations.depthStrong,
    confirmations.microStrong,
    htf.bullishVotes>=2,
  ].filter(Boolean).length;
  const thesisQuality=Math.min(100,Math.round(Number(setup.setupQuality||70)+optional*2));
  const hist=calibration?.[setup.setup]||{trades:0,wins:0,winRate:0,bayesianWinRate:0.5,status:"LOW_SAMPLE",expectancy:0};
  return {
    passed,
    decision:passed?"READY":"WAIT",
    thesisQuality,
    setup:setup.setup,
    strategy:setup.strategy,
    premise:setup.patternEvidence,
    invalidation:{
      price:stop,
      reason:setup.invalidationReason,
    },
    targets:{
      tp1:targets.target1,
      tp2:targets.target2,
      source:targets.source,
      grossRR1,
      grossRR2,
    },
    context:{
      marketRegime,
      higherTimeframes:htf,
    },
    confirmations:{
      ...confirmations,
      required,
    },
    calibration:{
      status:hist.status,
      sampleSize:hist.trades,
      historicalWinRate:hist.trades?hist.winRate:null,
      calibratedWinProbability:hist.status==="CALIBRATED"?hist.bayesianWinRate:null,
      expectancy:hist.trades?hist.expectancy:null,
    },
  };
}
