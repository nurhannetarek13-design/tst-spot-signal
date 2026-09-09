from pathlib import Path

# Deployment safety version 3: keep the pre-BUY book/spread/stale-price guard
# and ensure execution TP is never rounded above the model target.
path = Path('src/buy-gateway-canonical.js')
s = path.read_text(encoding='utf-8')

helper_anchor = '''function roundTick(v,tick,mode="nearest") { const d=decimals(step) if False else decimals(tick); const n=v/tick; const k=mode==="down"?Math.floor(n+1e-12):mode==="up"?Math.ceil(n-1e-12):Math.round(n); return Number((k*tick).toFixed(d)); }\n\nasync function executeBuyThenProtect(env,id,p) {\n'''
# The source currently has the same function without the no-op Python-only text above;
# use a direct literal anchor for JS.
helper_anchor = '''function roundTick(v,tick,mode="nearest") { const d=decimals(tick), n=v/tick; const k=mode==="down"?Math.floor(n+1e-12):mode==="up"?Math.ceil(n-1e-12):Math.round(n); return Number((k*tick).toFixed(d)); }\n\nasync function executeBuyThenProtect(env,id,p) {\n'''
helper_replacement = '''function roundTick(v,tick,mode="nearest") { const d=decimals(tick), n=v/tick; const k=mode==="down"?Math.floor(n+1e-12):mode==="up"?Math.ceil(n-1e-12):Math.round(n); return Number((k*tick).toFixed(d)); }\n\nasync function publicBookTicker(symbol) {\n  const bases=["https://data-api.binance.vision","https://api-gcp.binance.com","https://api1.binance.com","https://api2.binance.com","https://api3.binance.com","https://api4.binance.com"];\n  let last="unavailable";\n  for (const base of bases) {\n    try {\n      const r=await fetch(`${base}/api/v3/ticker/bookTicker?symbol=${encodeURIComponent(symbol)}`,{headers:{"cache-control":"no-store"}});\n      const text=await r.text();\n      if(r.ok) return JSON.parse(text||"{}");\n      last=`${r.status}`;\n    } catch(e) { last=String(e?.message||e); }\n  }\n  throw new Error(`BINANCE_BOOK_FAILED:${last}`);\n}\n\nasync function executionPriceGate(symbol, referenceEntry, referenceStop, referenceTarget) {\n  const book=await publicBookTicker(symbol);\n  const ask=Number(book.askPrice||0), bid=Number(book.bidPrice||0), ref=Number(referenceEntry||0), stop=Number(referenceStop||0), target=Number(referenceTarget||0);\n  if(!(ask>0 && bid>0 && ask>=bid && ref>0 && stop>0 && target>ref)) throw new Error("EXECUTION_BOOK_INVALID");\n  const mid=(ask+bid)/2, spread=(ask-bid)/mid, deviation=ask/ref-1;\n  if(spread>0.0012) throw new Error(`EXECUTION_SPREAD_TOO_WIDE:${(spread*100).toFixed(3)}%`);\n  if(ask>=target) throw new Error("SETUP_ALREADY_AT_OR_ABOVE_TARGET");\n  if(ask<=stop) throw new Error("SETUP_ALREADY_AT_OR_BELOW_STOP");\n  if(deviation>0.0035) throw new Error(`STALE_PRICE_CHASE:${(deviation*100).toFixed(3)}%`);\n  if(deviation<-0.0060) throw new Error(`SETUP_DETERIORATED:${(deviation*100).toFixed(3)}%`);\n  return {ask,bid,spreadPct:spread*100,deviationPct:deviation*100};\n}\n\nasync function executeBuyThenProtect(env,id,p) {\n'''
if 'async function executionPriceGate(' not in s:
    if helper_anchor not in s:
        raise SystemExit('execution-safety patch: helper anchor missing')
    s = s.replace(helper_anchor, helper_replacement, 1)

buy_old = '''async function executeBuyThenProtect(env,id,p) {\n  const buy=await relayMake(env,{signal_id:id,action:"BUY",symbol:p.symbol,quote_amount_usdt:Number(p.confirmedQuoteUSDT||p.recommendedUSDT),take_profit_price:Number(p.target),stop_loss_price:Number(p.stop),confirmed:true,dry_run:false,timestamp:Math.floor(Date.now()/1000)});\n'''
buy_new = '''async function executeBuyThenProtect(env,id,p) {\n  const priceGate=await executionPriceGate(p.symbol,p.entry,p.stop,p.target);\n  const buy=await relayMake(env,{signal_id:id,action:"BUY",symbol:p.symbol,quote_amount_usdt:Number(p.confirmedQuoteUSDT||p.recommendedUSDT),reference_price:Number(p.entry),signal_created_at_ms:Number(p.createdAt||0),execution_ask:priceGate.ask,execution_spread_pct:priceGate.spreadPct,take_profit_price:Number(p.target),stop_loss_price:Number(p.stop),confirmed:true,dry_run:false,timestamp:Math.floor(Date.now()/1000)});\n'''
if 'execution_ask:priceGate.ask' not in s:
    if buy_old not in s:
        raise SystemExit('execution-safety patch: BUY anchor missing')
    s = s.replace(buy_old, buy_new, 1)

# Never round TP above the strategy/model target. When one tick is <=0.10% of
# target, place one extra tick below the floor to reduce near-miss reversals.
tp_old = '''  const tp=roundTick(avg*(Number(p.target)/Number(p.entry)),tick,"up");\n  const sl=roundTick(avg*(Number(p.stop)/Number(p.entry)),tick,"down");\n'''
tp_new = '''  const modelTp=avg*(Number(p.target)/Number(p.entry));\n  let tp=roundTick(modelTp,tick,"down");\n  if(tick/modelTp<=0.001 && tp-tick>avg) tp=roundTick(tp-tick,tick,"down");\n  const sl=roundTick(avg*(Number(p.stop)/Number(p.entry)),tick,"down");\n'''
if 'const modelTp=avg*' not in s:
    if tp_old not in s:
        raise SystemExit('execution-safety patch: TP rounding anchor missing')
    s = s.replace(tp_old, tp_new, 1)

oco_old = '''    oco=await relayMake(env,{signal_id:id,action:"OCO",symbol:p.symbol,quantity:qty,take_profit_price:tp,stop_loss_price:sl,stop_limit_price:slLimit,confirmed:true,dry_run:false,timestamp:Math.floor(Date.now()/1000)});\n'''
oco_new = '''    oco=await relayMake(env,{signal_id:id,action:"OCO",symbol:p.symbol,quantity:qty,take_profit_price:tp,model_take_profit_price:modelTp,stop_loss_price:sl,stop_limit_price:slLimit,confirmed:true,dry_run:false,timestamp:Math.floor(Date.now()/1000)});\n'''
if 'model_take_profit_price:modelTp' not in s:
    if oco_old not in s:
        raise SystemExit('execution-safety patch: OCO payload anchor missing')
    s = s.replace(oco_old, oco_new, 1)

path.write_text(s, encoding='utf-8')
print('[cloudflare-execution-safety] OK pre-BUY book/spread/stale-price gate + TP floor/buffer enabled')
