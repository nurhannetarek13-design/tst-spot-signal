import {
  makeReadOnlyHeartbeat,
  MAKE_EXECUTION_ROUTE,
} from "./make-live-client.js";
import {
  manualBuyAndProtectViaSupabase,
  supabaseReadOnlyHeartbeat,
  supabaseReadOnlyReconcile,
  supabaseExecutionConfigured,
  supabaseDryRunExecution,
  supabaseReconcileActiveTrades,
} from "./supabase-live-client.js";

export function executionProvider(env = {}) {
  const requested = String(env.EXECUTION_PROVIDER || "SUPABASE_V2").trim().toUpperCase();
  return requested === "SUPABASE_V2" ? "SUPABASE_V2" : "MAKE_V2";
}

export function executionRoute(env = {}) {
  return executionProvider(env) === "SUPABASE_V2"
    ? "CLOUDFLARE_SIGNED_SUPABASE_BINANCE"
    : "CLOUDFLARE_HMAC_MAKE";
}

export function executorConfigured(env = {}) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseExecutionConfigured(env);
  return String(env.MAKE_EXECUTOR_V2_READY || "").toLowerCase() === "true";
}

export function executionOwner(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" ? "SUPABASE_EXECUTOR_V2" : "MAKE_EXECUTOR_V2";
}

export function routeVersion(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" ? "supabase-v2" : MAKE_EXECUTION_ROUTE.version;
}

export async function executionReadOnlyReconcile(env) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseReadOnlyReconcile(env);
  return null;
}

export async function executionReadOnlyHeartbeat(env) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseReadOnlyHeartbeat(env);
  return makeReadOnlyHeartbeat(env);
}

export async function manualBuyAndProtect(env, input) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", noOrderSent:true };
  }
  return manualBuyAndProtectViaSupabase(env, input);
}

export function executionRouteIds(env = {}) {
  if (executionProvider(env) === "SUPABASE_V2") {
    return { buy: "SUPABASE_BUY_V2", oco: "SUPABASE_OCO_V2" };
  }
  return { buy: MAKE_EXECUTION_ROUTE.buy.id, oco: MAKE_EXECUTION_ROUTE.oco.id };
}


export async function executionDryRun(env, input) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseDryRunExecution(env, input);
}


export async function executionReconcileActiveTrades(env) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseReconcileActiveTrades(env);
}
