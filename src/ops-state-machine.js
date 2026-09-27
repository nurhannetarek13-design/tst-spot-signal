export function deriveOpsState({
  now,
  heartbeats = {},
  previous = { state: "WARMING_UP", since: now },
  reconciliation = null,
  scheduler = null,
  heartbeatStaleMs = 20 * 60 * 1000,
  recoveryHoldMs = 60 * 1000,
  warmupMs = 2 * 60 * 1000,
}) {
  const hbAt = (name) => {
    const row = heartbeats?.[name];
    return Number(typeof row === "number" ? row : row?.at || 0);
  };
  const critical = ["scanner", "market-data", "strategy", "risk", "watchdog", "binance-readonly", "reconciler", "protection"];
  const stale = critical.filter((c) => !hbAt(c) || now - hbAt(c) > heartbeatStaleMs);

  let state = "HEALTHY";
  let reason = null;
  if (stale.length) {
    state = "DEGRADED";
    reason = `STALE_HEARTBEAT:${stale.join(",")}`;
  } else if (reconciliation?.ok === false) {
    state = "PROTECTION_ONLY";
    reason = reconciliation.reason || "RECONCILIATION_FAILED";
  } else if (["DEGRADED", "PROTECTION_ONLY"].includes(previous.state)) {
    state = "RECOVERING";
    reason = "RECOVERY_STABILIZATION";
  } else if (previous.state === "RECOVERING") {
    if (now - Number(previous.since || now) < recoveryHoldMs) {
      state = "RECOVERING";
      reason = "RECOVERY_STABILIZATION";
    } else {
      state = "RECONCILING";
      reason = "POST_RECOVERY_RECONCILIATION";
    }
  } else if (previous.state === "RECONCILING") {
    if (!reconciliation || now - Number(reconciliation.at || 0) > heartbeatStaleMs) {
      state = "RECONCILING";
      reason = "WAITING_FOR_FRESH_RECONCILIATION";
    } else if (reconciliation.ok !== true) {
      state = "PROTECTION_ONLY";
      reason = reconciliation.reason || "RECONCILIATION_FAILED";
    } else {
      state = "WARMING_UP";
      reason = "POST_RECOVERY_WARMUP";
    }
  } else if (previous.state === "WARMING_UP" && now - Number(previous.since || now) < warmupMs) {
    state = "WARMING_UP";
    reason = "WARMUP_WINDOW";
  }

  return {
    state,
    reason,
    stale,
    since: previous.state === state ? previous.since : now,
    checkedAt: now,
    schedulerLastAt: Number(scheduler?.at || 0) || null,
    reconciliationAt: Number(reconciliation?.at || 0) || null,
    newEntriesAllowed: false,
    liveTrading: false,
    autonomousExecution: false,
  };
}
