from pathlib import Path

p = Path("src/buy-gateway.js")
s = p.read_text(encoding="utf-8")

old = '''  const text = await r.text();
  let row = {};
  try { row = JSON.parse(text || "{}"); } catch { row = { ok: false, status: "BAD_RELAY_RESPONSE" }; }
  if (!r.ok || row.ok !== true) {
    const detail = row?.upstream?.code ? `${row.upstream.code} ${row.upstream.msg || ""}` : (row.reason || row.status || r.status);
    throw new Error(`BINANCE_RELAY_ERROR: ${detail}`);
  }
'''

new = '''  const text = await r.text();
  let row = {};
  try { row = JSON.parse(text || "{}"); } catch {
    const preview = String(text || "")
      .replace(/[A-Za-z0-9_-]{20,}/g, "[redacted]")
      .slice(0, 240);
    row = {
      ok: false,
      status: "BAD_RELAY_RESPONSE",
      relayHttpStatus: r.status,
      relayContentType: r.headers.get("content-type") || "",
      relayBodyPreview: preview,
    };
  }
  if (!r.ok || row.ok !== true) {
    const detail = row?.upstream?.code
      ? `${row.upstream.code} ${row.upstream.msg || ""}`
      : row.status === "BAD_RELAY_RESPONSE"
        ? `BAD_RELAY_RESPONSE http=${row.relayHttpStatus} type=${row.relayContentType} body=${row.relayBodyPreview}`
        : (row.reason || row.status || r.status);
    throw new Error(`BINANCE_RELAY_ERROR: ${detail}`);
  }
'''

if old not in s:
    if "relayBodyPreview" in s:
        print("relay diagnostics already present")
        raise SystemExit(0)
    raise SystemExit("signed relay response parser marker missing")

p.write_text(s.replace(old, new), encoding="utf-8")
print("safe relay diagnostics materialized")
