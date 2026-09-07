from pathlib import Path

# Product mode: Telegram should stay quiet unless there is an actionable trade
# opportunity or an execution result. Scanning/balance monitoring continues
# silently in the background.

edge = Path("src/edge-worker.js")
s = edge.read_text()

scheduled_variants = [
    '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await sendPeriodicScanDigest(env); await scan(env,true); })()); },',
    '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,true); })()); },',
]
quiet_scheduled = '  async scheduled(event,env,ctx){ ctx.waitUntil((async()=>{ await monitorUnifiedDerivative(env); await monitorPaper(env); await scan(env,false); })()); },'
if quiet_scheduled not in s:
    for old in scheduled_variants:
        if old in s:
            s = s.replace(old, quiet_scheduled, 1)
            break
    else:
        raise SystemExit("edge scheduled handler changed; refusing unsafe product-mode patch")

# Keep paper bookkeeping, but do not send PAPER CLOSE messages to Telegram.
paper_notice = '      await telegram(env,[`${pnl>=0?"✅":"🔴"} PAPER CLOSE — ${p.symbol.replace("USDT","/USDT")}`'
if paper_notice in s:
    s = s.replace(paper_notice, '      if(false) await telegram(env,[`${pnl>=0?"✅":"🔴"} PAPER CLOSE — ${p.symbol.replace("USDT","/USDT")}`', 1)

edge.write_text(s)

buy = Path("src/buy-gateway.js")
b = buy.read_text()
# Balance is still refreshed every five minutes and after execution, but no
# standalone balance digest is sent. Balance remains embedded in opportunity data.
b = b.replace('await refreshBalance(env,true);', 'await refreshBalance(env,false);')
buy.write_text(b)

print("Product mode enabled: Telegram sends actionable opportunities/execution results only")
