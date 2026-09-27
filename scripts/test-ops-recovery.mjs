import assert from "node:assert/strict";
import { deriveOpsState } from "../src/ops-state-machine.js";

const STALE = 20 * 60 * 1000;
const HOLD = 60 * 1000;
const WARM = 2 * 60 * 1000;
const names = ["scanner","market-data","strategy","risk","watchdog","binance-readonly","reconciler","protection"];
const hb = (at) => Object.fromEntries(names.map((n) => [n, { at }]));
const base = 1_790_507_000_000;

let s = deriveOpsState({
  now: base,
  heartbeats: hb(base),
  previous: { state: "WARMING_UP", since: base },
  reconciliation: { ok: true, at: base },
  scheduler: { at: base },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "WARMING_UP");
assert.equal(s.newEntriesAllowed, false);

s = deriveOpsState({
  now: base + WARM + 1,
  heartbeats: hb(base + WARM + 1),
  previous: { state: "WARMING_UP", since: base },
  reconciliation: { ok: true, at: base + WARM + 1 },
  scheduler: { at: base + WARM + 1 },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "HEALTHY");

s = deriveOpsState({
  now: base + STALE + 10_000,
  heartbeats: hb(base),
  previous: { state: "HEALTHY", since: base },
  reconciliation: { ok: true, at: base },
  scheduler: { at: base },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "DEGRADED");
assert.ok(s.stale.length > 0);

const recoveredAt = base + STALE + 20_000;
s = deriveOpsState({
  now: recoveredAt,
  heartbeats: hb(recoveredAt),
  previous: { state: "DEGRADED", since: recoveredAt - 1000 },
  reconciliation: { ok: true, at: recoveredAt },
  scheduler: { at: recoveredAt },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "RECOVERING");

s = deriveOpsState({
  now: recoveredAt + HOLD + 1,
  heartbeats: hb(recoveredAt + HOLD + 1),
  previous: { state: "RECOVERING", since: recoveredAt },
  reconciliation: { ok: true, at: recoveredAt + HOLD + 1 },
  scheduler: { at: recoveredAt + HOLD + 1 },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "RECONCILING");

s = deriveOpsState({
  now: recoveredAt + HOLD + 2,
  heartbeats: hb(recoveredAt + HOLD + 2),
  previous: { state: "RECONCILING", since: recoveredAt + HOLD + 1 },
  reconciliation: { ok: true, at: recoveredAt + HOLD + 2 },
  scheduler: { at: recoveredAt + HOLD + 2 },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "WARMING_UP");

s = deriveOpsState({
  now: recoveredAt + HOLD + WARM + 10,
  heartbeats: hb(recoveredAt + HOLD + WARM + 10),
  previous: { state: "WARMING_UP", since: recoveredAt + HOLD + 2 },
  reconciliation: { ok: true, at: recoveredAt + HOLD + WARM + 10 },
  scheduler: { at: recoveredAt + HOLD + WARM + 10 },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "HEALTHY");

s = deriveOpsState({
  now: base,
  heartbeats: hb(base),
  previous: { state: "HEALTHY", since: base - 10_000 },
  reconciliation: { ok: false, reason: "PROTECTION_MISMATCH", at: base },
  scheduler: { at: base },
  heartbeatStaleMs: STALE,
  recoveryHoldMs: HOLD,
  warmupMs: WARM,
});
assert.equal(s.state, "PROTECTION_ONLY");
assert.equal(s.newEntriesAllowed, false);

console.log("OPS_RECOVERY_SELFTEST_PASS");
