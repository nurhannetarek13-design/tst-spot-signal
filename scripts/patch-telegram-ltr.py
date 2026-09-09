from pathlib import Path

p = Path("src/buy-gateway-canonical.js")
s = p.read_text(encoding="utf-8")

fmt_marker = '''function fmt(v) {
  return Number(v || 0).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 });
}
'''
ltr_helper = fmt_marker + '''function ltrText(v) {
  return `\\u2066${String(v)}\\u2069`;
}
function ltrFmt(v) {
  return ltrText(fmt(v));
}
'''

if "function ltrFmt(v)" not in s:
    if fmt_marker not in s:
        raise SystemExit("fmt marker missing")
    s = s.replace(fmt_marker, ltr_helper, 1)

old = '''text:`🚨 CONFIRMED BUY — ${symbol} — SPOT\\n💵 ${fmt(requested)} USDT\\n💲 Entry ref ${fmt(entry)}\\n🎯 TP ${fmt(target)}\\n🛑 SL ${fmt(stop)}\\n⭐ Score ${Number.isFinite(score)?score:"—"}/100\\n\\n⚡ CONFIRM BUY ينفذ Market Buy عبر Make، وبعد الـfill يحسب TP/SL على سعر التنفيذ الحقيقي ويحط OCO تلقائيًا.`'''
new = '''text:`🚨 CONFIRMED BUY — ${symbol} — SPOT\\n💵 ${ltrFmt(requested)} USDT\\n💲 Entry ref ${ltrFmt(entry)}\\n🎯 TP ${ltrFmt(target)}\\n🛑 SL ${ltrFmt(stop)}\\n⭐ Score ${ltrText(Number.isFinite(score)?`${score}/100`:"—/100")}\\n\\n⚡ CONFIRM BUY ينفذ Market Buy عبر Make، وبعد الـfill يحسب TP/SL على سعر التنفيذ الحقيقي ويحط OCO تلقائيًا.`'''

if old in s:
    s = s.replace(old, new, 1)
elif "💵 ${ltrFmt(requested)} USDT" not in s:
    raise SystemExit("confirmed message marker missing")

old_post = '''💵 Spent ${fmt(r.quoteSpent)} USDT\\n💲 Avg fill ${fmt(r.avg)}\\n📦 Protected qty ${fmt(r.qty)}\\n🎯 TP ${fmt(r.tp)}\\n🛑 SL trigger ${fmt(r.sl)}\\n🛑 SL limit ${fmt(r.slLimit)}'''
new_post = '''💵 Spent ${ltrFmt(r.quoteSpent)} USDT\\n💲 Avg fill ${ltrFmt(r.avg)}\\n📦 Protected qty ${ltrFmt(r.qty)}\\n🎯 TP ${ltrFmt(r.tp)}\\n🛑 SL trigger ${ltrFmt(r.sl)}\\n🛑 SL limit ${ltrFmt(r.slLimit)}'''
if old_post in s:
    s = s.replace(old_post, new_post, 1)

if "ltrFmt(requested)" not in s or "ltrFmt(entry)" not in s or "ltrFmt(target)" not in s or "ltrFmt(stop)" not in s:
    raise SystemExit("LTR numeric patch incomplete")

p.write_text(s, encoding="utf-8")
print("patched Telegram numeric LTR isolation")

# Time-monitor patch: manual Binance exits are invisible to the Cloudflare
# reminder state unless the user explicitly closes tracking. Add a SOLD button
# after the exit flow, and retire the already-sold UUSDT legacy position once.
m = Path("src/buy-gateway-time-monitor.js")
t = m.read_text(encoding="utf-8")

# The user explicitly confirmed UUSDT was sold manually. U/stablecoins are now
# excluded from the scanner, so automatically retire any stale UUSDT monitor row.
legacy_marker = '''  for (const p of rows) {
    if (p.closed) continue;
    const age = now - Number(p.openedAt || now);
'''
legacy_repl = '''  for (const p of rows) {
    if (p.closed) continue;
    if (p.symbol === "UUSDT") {
      p.closed = true;
      p.closedAt = now;
      p.closedReason = "manual-user-sale";
      changed = true;
      console.log(`[time-monitor] ${p.symbol} tracking closed after confirmed manual sale`);
      continue;
    }
    const age = now - Number(p.openedAt || now);
'''
if 'closedReason = "manual-user-sale"' not in t:
    if legacy_marker not in t:
        raise SystemExit("time monitor legacy-close marker missing")
    t = t.replace(legacy_marker, legacy_repl, 1)

old_keyboard = '''    reply_markup: {
      inline_keyboard: [[{ text: `📤 OPEN ${intent.symbol} ON BINANCE`, url: binanceTradeUrl(intent.symbol) }]],
    },
'''
new_keyboard = '''    reply_markup: {
      inline_keyboard: [
        [{ text: `📤 OPEN ${intent.symbol} ON BINANCE`, url: binanceTradeUrl(intent.symbol) }],
        [{ text: "✅ SOLD — STOP REMINDERS", callback_data: `EXITDONE:${id}` }],
      ],
    },
'''
if 'SOLD — STOP REMINDERS' not in t:
    if old_keyboard not in t:
        raise SystemExit("time monitor exit-ready keyboard marker missing")
    t = t.replace(old_keyboard, new_keyboard, 1)

handler_marker = '''async function handleExitCancel(request, env) {
'''
handler = '''async function handleExitDone(request, env) {
  const u = await request.clone().json().catch(() => null);
  const q = u?.callback_query;
  if (!q || String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return null;
  const [action, id] = String(q.data || "").split(":");
  if (action !== "EXITDONE") return null;
  const rows = await readOpenPositions(env);
  const p = rows.find((x) => x.id === id);
  if (!p || p.closed) {
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Tracking already closed", show_alert: true });
    return new Response("ok");
  }
  p.closed = true;
  p.closedAt = Date.now();
  p.closedReason = "manual-exit-confirmed";
  await writeOpenPositions(env, rows);
  await putState(env, `exit-intent:${id}`, null, 1);
  await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Closed — reminders stopped" });
  await tg(env, "sendMessage", {
    chat_id: String(env.TELEGRAM_CHAT_ID),
    text: `✅ TRACKING CLOSED — ${p.symbol}\\nمش هبعت تذكيرات EXIT/HOLD للصفقة دي تاني.`,
  });
  return new Response("ok");
}

'''
if 'async function handleExitDone' not in t:
    if handler_marker not in t:
        raise SystemExit("time monitor exit-done handler marker missing")
    t = t.replace(handler_marker, handler + handler_marker, 1)

route_marker = '''      if (data.startsWith("EXITCANCEL:")) {
'''
route_insert = '''      if (data.startsWith("EXITDONE:")) {
        const done = await handleExitDone(request, env);
        if (done) return done;
      }
'''
if 'data.startsWith("EXITDONE:")' not in t:
    if route_marker not in t:
        raise SystemExit("time monitor exit-done route marker missing")
    t = t.replace(route_marker, route_insert + route_marker, 1)

for required in [
    'SOLD — STOP REMINDERS',
    'async function handleExitDone',
    'data.startsWith("EXITDONE:")',
    'closedReason = "manual-user-sale"',
]:
    if required not in t:
        raise SystemExit(f"time monitor sold patch incomplete: {required}")

m.write_text(t, encoding="utf-8")
print("patched time monitor manual-sale close + SOLD stop-reminders callback")
