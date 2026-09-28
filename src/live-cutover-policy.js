export const LIVE_POLICY = Object.freeze({
  liveExecutionEnabled: false,
  autonomousEnabled: false,
  maxOrderUSDT: 5.5,
  maxOpenPositions: 1,
  dailyLossCapUSDT: 0.5,
  emergencyKillSwitch: false,
});

export function readLivePolicy(env = {}) {
  const liveExecutionEnabled = String(env.LIVE_EXECUTION_ENABLED || "").toLowerCase() === "true";
  const autonomousEnabled = String(env.AUTONOMOUS_ENABLED || "").toLowerCase() === "true";
  const requestedOrder = Number(env.MAX_ORDER_USDT ?? 5.5);
  const requestedPositions = Number(env.MAX_OPEN_POSITIONS ?? 1);
  const requestedDailyLoss = Number(env.DAILY_LOSS_CAP_USDT ?? 0.5);
  const emergencyKillSwitch = String(env.EMERGENCY_KILL_SWITCH || "").toLowerCase() === "true";
  return Object.freeze({
    liveExecutionEnabled,
    autonomousEnabled,
    maxOrderUSDT: Number.isFinite(requestedOrder) && requestedOrder > 0 ? Math.min(5.5, requestedOrder) : 5.5,
    maxOpenPositions: Number.isFinite(requestedPositions) ? Math.min(1, Math.max(0, Math.floor(requestedPositions))) : 1,
    dailyLossCapUSDT: Number.isFinite(requestedDailyLoss) && requestedDailyLoss > 0 ? Math.min(0.5, requestedDailyLoss) : 0.5,
    emergencyKillSwitch,
  });
}

export function evaluateGoNoGo({
  supervisorState,
  reconciliationOk,
  snapshotFresh,
  watchdogHealthy,
  binanceConnectionOk,
  executionRouteHealthy,
  executorOwnershipOk = true,
  unknownOrders = 0,
  unprotectedPositions = 0,
  dailyLossUSDT = 0,
  policy = LIVE_POLICY,
} = {}) {
  const operationalChecks = {
    SUPERVISOR_HEALTHY: supervisorState === "HEALTHY",
    RECONCILIATION_OK: reconciliationOk === true,
    SNAPSHOT_FRESH: snapshotFresh === true,
    WATCHDOG_HEALTHY: watchdogHealthy === true,
    BINANCE_CONNECTION_OK: binanceConnectionOk === true,
    EXECUTION_ROUTE_HEALTHY: executionRouteHealthy === true,
    EXECUTOR_OWNERSHIP_OK: executorOwnershipOk === true,
    NO_UNKNOWN_ORDERS: Number(unknownOrders || 0) === 0,
    NO_UNPROTECTED_POSITIONS: Number(unprotectedPositions || 0) === 0,
    DAILY_RISK_AVAILABLE: Number(dailyLossUSDT || 0) > -Math.abs(Number(policy.dailyLossCapUSDT || 0)),
    EMERGENCY_KILL_SWITCH_CLEAR: policy.emergencyKillSwitch !== true,
  };
  const activationChecks = {
    LIVE_EXECUTION_ENABLED: policy.liveExecutionEnabled === true,
    AUTONOMOUS_ENABLED: policy.autonomousEnabled === true,
  };
  const failedOperational = Object.entries(operationalChecks).filter(([, ok]) => !ok).map(([name]) => name);
  const failedActivation = Object.entries(activationChecks).filter(([, ok]) => !ok).map(([name]) => name);
  const operationalGo = failedOperational.length === 0;
  const activationAllowed = operationalGo && failedActivation.length === 0;
  return {
    go: operationalGo,
    status: operationalGo ? "GO" : "NO_GO",
    activationAllowed,
    activationStatus: activationAllowed ? "LIVE_ALLOWED" : "LIVE_DISABLED",
    failed: failedOperational,
    activationFailed: failedActivation,
    checks: operationalChecks,
    activationChecks,
    policy: {
      maxOrderUSDT: Number(policy.maxOrderUSDT),
      maxOpenPositions: Number(policy.maxOpenPositions),
      dailyLossCapUSDT: Number(policy.dailyLossCapUSDT),
    },
  };
}

export function resolveUnknownExecution({ transportTimedOut, reconciledOrder = null } = {}) {
  if (!transportTimedOut) return { status: "NOT_UNKNOWN", mayResend: false };
  if (!reconciledOrder) return { status: "UNKNOWN_RECONCILIATION_REQUIRED", mayResend: false };
  if (String(reconciledOrder.status || "") === "FILLED") return { status: "FILLED_FOUND_BY_RECONCILIATION", mayResend: false };
  if (["NEW", "PARTIALLY_FILLED"].includes(String(reconciledOrder.status || ""))) {
    return { status: "ORDER_EXISTS_DO_NOT_RESEND", mayResend: false };
  }
  if (["REJECTED", "EXPIRED", "CANCELED"].includes(String(reconciledOrder.status || ""))) {
    return { status: "TERMINAL_REJECTED_REVIEW_REQUIRED", mayResend: false };
  }
  return { status: "UNKNOWN_RECONCILIATION_REQUIRED", mayResend: false };
}

export function protectionDecision({ buyStatus, executedQty, ocoAccepted } = {}) {
  const qty = Number(executedQty || 0);
  if (buyStatus === "REJECTED" || qty <= 0) return { action: "NO_POSITION", protectedQty: 0 };
  if (ocoAccepted === true) return { action: "PROTECTED", protectedQty: qty };
  return { action: "EMERGENCY_CLOSE_REQUIRED", protectedQty: 0, blockNewTrades: true };
}

export function accountingParity({ bot = {}, binance = {} } = {}) {
  const tol = 1e-10;
  const diffs = {
    fillQty: Number(bot.fillQty || 0) - Number(binance.fillQty || 0),
    quoteQty: Number(bot.quoteQty || 0) - Number(binance.quoteQty || 0),
    commission: Number(bot.commission || 0) - Number(binance.commission || 0),
    netPnl: Number(bot.netPnl || 0) - Number(binance.netPnl || 0),
    residualQty: Number(bot.residualQty || 0) - Number(binance.residualQty || 0),
  };
  const clean = Object.values(diffs).every((x) => Math.abs(x) <= tol);
  return { clean, status: clean ? "ACCOUNTING_PARITY_OK" : "ACCOUNTING_PARITY_MISMATCH", diffs };
}
