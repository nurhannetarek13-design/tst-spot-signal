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
