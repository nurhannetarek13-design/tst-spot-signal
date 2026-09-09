import canonicalWorker, { SignalState } from "./buy-gateway-canonical.js";
export { SignalState };

const REVIEW_45_MS = 45 * 60 * 1000;
const REVIEW_75_MS = 75 * 60 * 1000;
const DECISION_REPEAT_MS = 15 * 60 * 1000;
const HOLD_EXTENSION_MS = 30 * 60 * 1000;
const POSITION_TTL_SEC = 24 * 60 * 60;
const EXIT_INTENT_TTL_SEC = 10 * 60;

function stateStub(env) {
  const id = env.STATE_COORDINATOR.idFromName("global");
  return env.STATE_COORDINATOR.get(id);
}
async function getState(env, key) {
  const r = await stateStub(env).fetch(`https://state/get?key=${encodeURIComponent(key)}`);
  return r.ok ? await r.json() : null;
}
async function putState(env, key, value, ttl = POSITION_TTL_SEC) {
  await stateStub(env).fetch(`https://state/put?key=${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
}
async function claimState(env, key, value, ttl) {
  const r = await stateStub(env).fetch(`https://state/claim?key=${encodeURIComponent(key)}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, expiresAt: Date.now() + ttl * 1000 }),
  });
  return r.ok;
}
async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const row = await r.json().catch(() => ({ ok: false }));
  if (!r.ok || row.ok !== true) throw new Error(`TELEGRAM_${method}_FAILED`);
  return row.result;
}
function fmt(v) {
  return Number(v || 0).toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 8 });
}
function binanceTradeUrl(symbol) {
  return `https://www.binance.com/en/trade/${String(symbol).replace("USDT", "_USDT")}?type=spot`;
}
async function publicPrice(symbol) {
  const bases = ["https://data-api.binance.vision", "https://api-gcp.binance.com", "https://api1.binance.com", "https://api2.binance.com"];
  let last = "unavailable";
  for (const base of bases) {
    try {
      const r = await fetch(`${base}/api/v3/ticker/price?symbol=${encodeURIComponent(symbol)}`);
      const row = await r.json();
      if (r.ok && Number(row?.price) > 0) return Number(row.price);
      last = String(row?.msg || r.status);
    } catch (e) { last = String(e?.message || e); }
  }
  throw new Error(`PRICE_FAILED:${last}`);
}
async function readOpenPositions(env) {
  const rows = await getState(env, "timed-open-positions");
  return Array.isArray(rows) ? rows : [];
}
async function writeOpenPositions(env, rows) {
  await putState(env, "timed-open-positions", rows.slice(-20));
}
async function registerConfirmedPosition(env, id) {
  await new Promise((resolve) => setTimeout(resolve, 1200));
  const result = await getState(env, `execution-result:${id}`);
  if (!result?.ok || result?.status !== "BOUGHT_AND_PROTECTED") return;
  const signal = await getState(env, `live-signal:${id}`) || await getState(env, `prepared:${id}`);
  if (!signal?.symbol) return;
  const rows = await readOpenPositions(env);
  if (rows.some((x) => x.id === id)) return;
  const openedAt = Number(result.at || Date.now());
  rows.push({
    id,
    symbol: signal.symbol,
    referenceEntry: Number(signal.entry || 0),
    target: Number(signal.target || 0),
    stop: Number(signal.stop || 0),
    openedAt,
    review45Sent: false,
    review75Sent: false,
    nextDecisionReminderAt: openedAt + REVIEW_75_MS,
    holdUntil: 0,
    closed: false,
  });
  await writeOpenPositions(env, rows);
}
async function markPositionClosed(env, id, reason = "manual-user-confirmed") {
  const rows = await readOpenPositions(env);
  const p = rows.find((x) => x.id === id && !x.closed);
  if (!p) return null;
  p.closed = true;
  p.closedAt = Date.now();
  p.closedReason = reason;
  p.nextDecisionReminderAt = 0;
  await writeOpenPositions(env, rows);
  return p;
}
async function sendReview(env, p, current, stage) {
  const basis = Number(p.referenceEntry || 0);
  const pnlPct = basis > 0 ? ((current / basis) - 1) * 100 : 0;
  const ageMin = Math.max(0, Math.round((Date.now() - Number(p.openedAt || Date.now())) / 60000));
  const status = pnlPct >= 0.35 ? "🟢 ماشية لصالحنا" : pnlPct <= -0.35 ? "🔴 ضعفت" : "🟡 شبه ثابتة";
  const headline = stage === 45 ? "⏱ 45m POSITION REVIEW" : "⏰ 75m+ DECISION DEADLINE";
  const action = stage === 45
    ? "دي مراجعة مبكرة. الـOCO شغال، ولو الصفقة فضلت مفتوحة هيوصل قرار عند 75 دقيقة."
    : "الصفقة عدّت مدة PRE-MOMENTUM المستهدفة. اختاري EXIT أو HOLD؛ ولو بعتيها يدويًا اختاري SOLD/CLOSED عشان أوقف المتابعة.";
  const keyboard = stage === 75 ? {
    inline_keyboard: [
      [{ text: "📤 EXIT NOW", callback_data: `EXITASK:${p.id}` }],
      [{ text: "⏳ HOLD 30m", callback_data: `HOLD30:${p.id}` }],
      [{ text: "✅ ALREADY SOLD / CLOSED", callback_data: `EXITDONE:${p.id}` }],
    ],
  } : undefined;
  await tg(env, "sendMessage", {
    chat_id: String(env.TELEGRAM_CHAT_ID),
    text: `${headline}\n${p.symbol}\n⏳ Age: ${ageMin} min\n💲 Current: ${fmt(current)}\n📍 Entry ref: ${fmt(basis)}\n📈 P/L ref: ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%\n${status}\n\n${action}`,
    ...(keyboard ? { reply_markup: keyboard } : {}),
  });
}
async function reviewPositions(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  const rows = await readOpenPositions(env);
  if (!rows.length) return;
  let changed = false;
  const now = Date.now();
  for (const p of rows) {
    if (p.closed) continue;

    // One-time cleanup for the legacy U position that was manually sold before
    // manual-close callbacks existed. U/stablecoins are already excluded from
    // new momentum entries, so this only stops the stale historical reminder.
    if (p.symbol === "UUSDT") {
      p.closed = true;
      p.closedAt = now;
      p.closedReason = "legacy-U-manual-sale-cleanup";
      p.nextDecisionReminderAt = 0;
      changed = true;
      console.log(`[time-monitor] ${p.symbol} stale legacy position marked closed`);
      continue;
    }

    const age = now - Number(p.openedAt || now);
    try {
      const current = await publicPrice(p.symbol);
      if ((Number(p.target) > 0 && current >= Number(p.target)) || (Number(p.stop) > 0 && current <= Number(p.stop))) {
        p.closed = true; changed = true; continue;
      }
      if (!p.review45Sent && age >= REVIEW_45_MS) {
        await sendReview(env, p, current, 45);
        p.review45Sent = true; changed = true;
      }
      const effectiveDeadline = Math.max(Number(p.openedAt || now) + REVIEW_75_MS, Number(p.holdUntil || 0));
      const nextReminder = Math.max(effectiveDeadline, Number(p.nextDecisionReminderAt || effectiveDeadline));
      if (now >= effectiveDeadline && now >= nextReminder) {
        await sendReview(env, p, current, 75);
        p.review75Sent = true;
        p.nextDecisionReminderAt = now + DECISION_REPEAT_MS;
        changed = true;
      }
    } catch (e) {
      console.log(`[time-monitor] ${p.symbol} review failed: ${String(e?.message || e)}`);
    }
  }
  if (changed) await writeOpenPositions(env, rows);
}
async function handleHoldCallback(request, env) {
  const u = await request.clone().json().catch(() => null);
  const q = u?.callback_query;
  if (!q || String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return null;
  const [action, id] = String(q.data || "").split(":");
  if (action !== "HOLD30") return null;
  const rows = await readOpenPositions(env);
  const p = rows.find((x) => x.id === id && !x.closed);
  if (!p) {
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Position not found/closed", show_alert: true });
    return new Response("ok");
  }
  p.holdUntil = Date.now() + HOLD_EXTENSION_MS;
  p.review75Sent = false;
  p.nextDecisionReminderAt = p.holdUntil;
  await writeOpenPositions(env, rows);
  await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Hold extended 30 minutes" });
  await tg(env, "sendMessage", { chat_id: String(env.TELEGRAM_CHAT_ID), text: `⏳ HOLD +30m — ${p.symbol}\nهراجعها تاني بعد 30 دقيقة. الـOCO يفضل شغال.` });
  return new Response("ok");
}
async function handleExitAsk(request, env) {
  const u = await request.clone().json().catch(() => null);
  const q = u?.callback_query;
  if (!q || String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return null;
  const [action, id] = String(q.data || "").split(":");
  if (action !== "EXITASK") return null;
  const rows = await readOpenPositions(env);
  const p = rows.find((x) => x.id === id && !x.closed);
  if (!p) {
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Position not found/closed", show_alert: true });
    return new Response("ok");
  }
  await putState(env, `exit-intent:${id}`, { id, symbol: p.symbol, createdAt: Date.now() }, EXIT_INTENT_TTL_SEC);
  await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Confirm exit first" });
  await tg(env, "sendMessage", {
    chat_id: String(env.TELEGRAM_CHAT_ID),
    text: `⚠️ CONFIRM EXIT — ${p.symbol}\n\nالزر ده مش هيبيع تلقائيًا. بعد التأكيد هيفتحلك نفس الزوج على Binance.\nالـOCO هيفضل شغال لحد ما تلغيه بنفسك، عشان الصفقة ما تفضلش من غير حماية لو ماكملتيش البيع.`,
    reply_markup: {
      inline_keyboard: [
        [{ text: "⚠️ CONFIRM EXIT", callback_data: `EXITOPEN:${id}` }],
        [{ text: "↩️ KEEP POSITION", callback_data: `EXITCANCEL:${id}` }],
      ],
    },
  });
  return new Response("ok");
}
async function handleExitOpen(request, env) {
  const u = await request.clone().json().catch(() => null);
  const q = u?.callback_query;
  if (!q || String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return null;
  const [action, id] = String(q.data || "").split(":");
  if (action !== "EXITOPEN") return null;
  const intent = await getState(env, `exit-intent:${id}`);
  if (!intent?.symbol) {
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Exit confirmation expired", show_alert: true });
    return new Response("ok");
  }
  const claimed = await claimState(env, `exit-open-lock:${id}`, { at: Date.now(), symbol: intent.symbol }, 120);
  if (!claimed) {
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Already opened — duplicate blocked", show_alert: true });
    return new Response("ok");
  }
  await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Opening Binance exit page" });
  await tg(env, "sendMessage", {
    chat_id: String(env.TELEGRAM_CHAT_ID),
    text: `📤 EXIT READY — ${intent.symbol}\n\n1) افتحي Binance من الزر.\n2) الغي OCO الخاص بالصفقة لو لسه مفتوح.\n3) Sell → Market → 100%.\n4) بعد ما البيع يتم، اضغطي SOLD / CLOSED عشان أوقف التذكيرات.\n\n⚠️ مفيش بيع اتنفذ من Telegram نفسه.`,
    reply_markup: {
      inline_keyboard: [
        [{ text: `📤 OPEN ${intent.symbol} ON BINANCE`, url: binanceTradeUrl(intent.symbol) }],
        [{ text: "✅ SOLD / CLOSED", callback_data: `EXITDONE:${id}` }],
      ],
    },
  });
  return new Response("ok");
}
async function handleExitDone(request, env) {
  const u = await request.clone().json().catch(() => null);
  const q = u?.callback_query;
  if (!q || String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return null;
  const [action, id] = String(q.data || "").split(":");
  if (action !== "EXITDONE") return null;
  const p = await markPositionClosed(env, id, "manual-user-confirmed");
  if (!p) {
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Already closed / not found" });
    return new Response("ok");
  }
  await putState(env, `exit-intent:${id}`, null, 1);
  await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Closed — reminders stopped" });
  await tg(env, "sendMessage", {
    chat_id: String(env.TELEGRAM_CHAT_ID),
    text: `✅ CLOSED — ${p.symbol}\nوقفت متابعة الصفقة وتذكيرات الـ75m.`,
  });
  return new Response("ok");
}
async function handleExitCancel(request, env) {
  const u = await request.clone().json().catch(() => null);
  const q = u?.callback_query;
  if (!q || String(q.message?.chat?.id || "") !== String(env.TELEGRAM_CHAT_ID || "")) return null;
  const [action, id] = String(q.data || "").split(":");
  if (action !== "EXITCANCEL") return null;
  await putState(env, `exit-intent:${id}`, null, 1);
  await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: "Exit cancelled — OCO stays active" });
  return new Response("ok");
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/telegram-webhook" && request.method === "POST") {
      const body = await request.clone().json().catch(() => null);
      const q = body?.callback_query;
      const data = String(q?.data || "");
      if (data.startsWith("HOLD30:")) {
        const held = await handleHoldCallback(request, env);
        if (held) return held;
      }
      if (data.startsWith("EXITASK:")) {
        const asked = await handleExitAsk(request, env);
        if (asked) return asked;
      }
      if (data.startsWith("EXITOPEN:")) {
        const opened = await handleExitOpen(request, env);
        if (opened) return opened;
      }
      if (data.startsWith("EXITDONE:")) {
        const done = await handleExitDone(request, env);
        if (done) return done;
      }
      if (data.startsWith("EXITCANCEL:")) {
        const cancelled = await handleExitCancel(request, env);
        if (cancelled) return cancelled;
      }
      const confirmId = data.startsWith("CONFIRM:") ? data.split(":")[1] : null;
      const response = await canonicalWorker.fetch(request, env, ctx);
      if (confirmId) ctx.waitUntil(registerConfirmedPosition(env, confirmId));
      return response;
    }
    return canonicalWorker.fetch(request, env, ctx);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(reviewPositions(env));
    if (typeof canonicalWorker.scheduled === "function") ctx.waitUntil(canonicalWorker.scheduled(event, env, ctx));
  },
};